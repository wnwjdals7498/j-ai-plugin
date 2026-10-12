"""Explicit local/hosted storage transition with quiescent local export."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import closing, nullcontext
from pathlib import Path

from ..errors import PmtError
from .. import handoff
from ..migration import MigrationCoordinator, _assert_quiescent
from ..resources import _reject_links
from ..storage_config import (_config_lock, _read_profile, _sha, configure_storage,
                              profile_environment_id, storage_path)
from . import client, connect, local_commands
from .credentials import ENV_NAME, has_credential_store, load_credential

_MAX_SWITCH_FILES = 10000
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_TERMINAL_RESULT_STATES = {"applied"}
_TERMINAL_RESOURCE_STATES = {"published"}


def _fail(message="Local or Host state has pending work; storage mode was not changed"):
    raise PmtError("switch_busy", message, 3)


def _readonly_connection(path):
    uri = Path(path).resolve(strict=True).as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.25)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _safe_regular(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise PmtError("switch_state_invalid", "Switch state path must be a regular file", 3)
    return info


def _scan_tree_for_files(root):
    root = Path(root)
    try:
        root.lstat()
    except FileNotFoundError:
        return []
    _reject_links(root)
    if not root.is_dir():
        raise PmtError("switch_state_invalid", "Pending state root must be a directory", 3)
    found = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            item = current_path / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PmtError("switch_state_invalid", "Pending state contains an unsafe directory", 3)
        for name in files:
            item = current_path / name
            _safe_regular(item)
            found.append(item)
            if len(found) > _MAX_SWITCH_FILES:
                raise PmtError("switch_state_invalid", "Pending state exceeds its file limit", 3)
    return found


def _read_legacy_claims(data_root):
    path = Path(data_root) / "easy-cli" / "claims.json"
    raw = local_commands._json_file(path, {})
    if not isinstance(raw, dict):
        raise PmtError("claim_store_invalid", "Legacy PMT claim store must be an object", 3)
    if raw:
        _fail()


def _read_outbox(path):
    try:
        with closing(_readonly_connection(path)) as connection:
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                _fail("Pending Host state is corrupt; storage mode was not changed")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "outbox_meta" not in tables or "pending_results" not in tables or "pending_resources" not in tables:
                _fail("Pending Host state schema is unknown; storage mode was not changed")
            version = connection.execute("SELECT value FROM outbox_meta WHERE key='schema_version'").fetchone()
            from ..pending import SCHEMA_VERSION
            if version is None or version[0] != str(SCHEMA_VERSION):
                _fail("Pending Host state schema is unsupported; storage mode was not changed")
            results = connection.execute(
                "SELECT COUNT(*) FROM pending_results WHERE state NOT IN (?)",
                tuple(_TERMINAL_RESULT_STATES)).fetchone()[0]
            resources = connection.execute(
                "SELECT COUNT(*) FROM pending_resources WHERE state NOT IN (?)",
                tuple(_TERMINAL_RESOURCE_STATES)).fetchone()[0]
            if results or resources:
                _fail()
    except (OSError, sqlite3.Error) as error:
        raise PmtError("switch_busy", "Pending Host state could not be verified; storage mode was not changed", 3) from error


def _check_pending_sidecars(data_root):
    data_root = Path(data_root)
    claims = local_commands._read_claims(data_root)
    if claims:
        _fail()
    _read_legacy_claims(data_root)

    hook_pending = data_root / "hook-pending"
    if _scan_tree_for_files(hook_pending):
        _fail("Pending Hook events must be replayed before switching storage modes")

    for root_name in ("hosted-pending", "host-pending"):
        for path in _scan_tree_for_files(data_root / root_name):
            if path.name == "outbox.sqlite3":
                _read_outbox(path)


def _check_local_database(data_root):
    data_root = Path(data_root)
    path = data_root / "pmt.sqlite3"
    info = _safe_regular(path)
    if info is None:
        return None
    _reject_links(data_root)
    try:
        with closing(_readonly_connection(path)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise PmtError("switch_database_invalid", "Local PMT database failed a read-only integrity check", 3)
            try:
                _assert_quiescent(connection, label="Source database")
            except PmtError as error:
                if error.code == "migration_source_not_quiescent":
                    _fail()
                raise
            owner = connection.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()
            if owner is not None and owner[0]:
                _fail("Local PMT database is under maintenance; storage mode was not changed")
    except PmtError:
        raise
    except sqlite3.OperationalError as error:
        if "locked" in str(error).casefold() or "busy" in str(error).casefold():
            raise PmtError("switch_busy", "Local PMT database is busy; storage mode was not changed", 3) from error
        raise PmtError("switch_database_invalid", "Local PMT database could not be checked read-only", 3) from error
    except (OSError, sqlite3.Error) as error:
        raise PmtError("switch_database_invalid", "Local PMT database could not be checked read-only", 3) from error
    return path


def _workspace_exports(profile):
    result = []
    for mapping in profile.get("workspace_mappings", []):
        root, repository_id, branch = (mapping.get("local_root"), mapping.get("repository_id"),
                                       mapping.get("branch"))
        if (not isinstance(root, str) or not Path(root).is_absolute()
                or not isinstance(repository_id, str) or not isinstance(branch, str)):
            raise PmtError("switch_mapping_invalid", "A saved workspace mapping cannot be exported safely", 3)
        result.append({"repository_id": repository_id, "local_workspace": root, "branch": branch})
    return result


def _inside(path, root):
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _validate_export_path(destination, data_root, config_root, profile):
    if not destination:
        return None
    path = Path(destination).expanduser().absolute()
    if path.suffix.casefold() != ".zip":
        raise PmtError("migration_destination_invalid", "Export destination must end with .zip", 2)
    if path.exists() or path.is_symlink():
        raise PmtError("migration_destination_exists", "Export destination already exists; originals are never overwritten", 3)
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or path.parent.is_symlink():
        raise PmtError("migration_destination_invalid", "Export parent must be an existing real directory", 3)
    _reject_links(parent)
    for root in (data_root, config_root):
        if _inside(path, root):
            raise PmtError("migration_destination_invalid", "Export destination cannot be inside PMT data or config roots", 3)
    for mapping in profile.get("workspace_mappings", []):
        workspace = mapping.get("local_root")
        if isinstance(workspace, str) and _inside(path, workspace):
            raise PmtError("migration_destination_invalid", "Export destination cannot be inside a mapped checkout", 3)
    return path


def _copy_verified_tree(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.exists():
        return
    _reject_links(source)
    if not source.is_dir():
        raise PmtError("migration_resource_unavailable", "Local resource root is not a directory", 3)
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            info = (current_path / name).lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PmtError("migration_resource_unavailable", "Local resources contain an unsafe link", 3)
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    for current, _directories, files in os.walk(source):
        for name in files:
            original = Path(current) / name
            target = destination / original.relative_to(source)
            if _file_sha(original) != _file_sha(target):
                raise PmtError("migration_resource_corrupt", "A cloned local resource did not match its source bytes", 5)


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _CloneDatabase:
    """Minimal DB interface for MigrationCoordinator, rooted only in a clone."""
    def __init__(self, data_root, config_root):
        self.root, self.config_root = Path(data_root).resolve(), Path(config_root).resolve()
        self.path = self.root / "pmt.sqlite3"
        self.busy_timeout_ms = 5000

    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _put_meta(connection, key, value):
        connection.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, value))


def _make_clone(source_path, source_root, config_root, clone_root):
    clone_root = Path(clone_root)
    clone_root.mkdir()
    clone_data, clone_config = clone_root / "data", clone_root / "config"
    clone_data.mkdir()
    clone_config.mkdir()
    shutil.copy2(Path(config_root) / "profile.json", clone_config / "profile.json")
    target_path = clone_data / "pmt.sqlite3"
    with closing(_readonly_connection(source_path)) as source, closing(sqlite3.connect(str(target_path))) as target:
        source.backup(target)
        target.commit()
    _copy_verified_tree(Path(source_root) / "resources", clone_data / "resources")
    _copy_verified_tree(Path(source_root) / "runner-spool", clone_data / "runner-spool")
    return _CloneDatabase(clone_data, clone_config)


def _zip_backup(bundle_root, destination):
    bundle_root = Path(bundle_root).resolve(strict=True)
    manifest = MigrationCoordinator().verify_backup(bundle_root)
    files = [bundle_root / "manifest.json", bundle_root / "transfer.sqlite3"]
    files.extend(bundle_root / item["bundle_path"] for item in manifest.get("resource_objects", []))
    names = {}
    total = 0
    for path in files:
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(bundle_root):
            raise PmtError("migration_bundle_invalid", "Export entry escaped the verified bundle", 5)
        name = path.relative_to(bundle_root).as_posix()
        digest = _file_sha(path)
        size = path.stat().st_size
        names[name] = (path, digest, size)
        total += size
    if total > _MAX_BUNDLE_BYTES:
        raise PmtError("migration_archive_too_large", "Export bundle exceeds the 64 MiB limit", 3)

    destination = Path(destination)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            for name in sorted(names):
                path, _digest, _size = names[name]
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, path.read_bytes())
        if temporary.stat().st_size > _MAX_BUNDLE_BYTES:
            raise PmtError("migration_archive_too_large", "Export ZIP exceeds the 64 MiB limit", 3)
        with zipfile.ZipFile(temporary, "r") as archive:
            info_list = archive.infolist()
            if len(info_list) != len(names) or len({item.filename for item in info_list}) != len(info_list):
                raise PmtError("migration_archive_invalid", "Export ZIP has duplicate or missing entries", 5)
            for info in info_list:
                expected = names.get(info.filename)
                mode = info.external_attr >> 16
                if (expected is None or info.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                        or ".." in Path(info.filename).parts or "\\" in info.filename
                        or archive.read(info) != expected[0].read_bytes()):
                    raise PmtError("migration_archive_invalid", "Export ZIP failed its manifest file verification", 5)
            if archive.testzip() is not None:
                raise PmtError("migration_archive_corrupt", "Export ZIP failed CRC verification", 5)
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise PmtError("migration_destination_exists", "Export destination already exists; it was not overwritten", 3) from error
        except OSError as error:
            raise PmtError("migration_destination_publish_failed", "Export ZIP could not be published without replacement", 4) from error
        return manifest
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _create_export(source_path, data_root, config_root, profile, destination):
    mappings = _workspace_exports(profile)
    with tempfile.TemporaryDirectory(prefix="pmt-e2-switch-") as temporary:
        clone_root = Path(temporary) / "source-clone"
        clone_db = _make_clone(source_path, data_root, config_root, clone_root)
        bundle_path = Path(temporary) / "bundle"
        MigrationCoordinator().create_backup(clone_db, bundle_path, mappings, source_kind="local")
        manifest = _zip_backup(bundle_path, destination)
        return {"bundle_id": manifest["bundle_id"], "manifest_sha256": manifest["manifest_sha256"],
                "file_count": 2 + len(manifest.get("resource_objects", []))}


def _local_preflight(config_root, data_root, profile):
    _check_pending_sidecars(data_root)
    return _check_local_database(data_root)


def _canonical_scopes(value, label):
    if not isinstance(value, list) or not value:
        raise PmtError("switch_state_unverifiable", f"{label} did not provide a complete scope list", 3)
    scopes = set()
    for scope in value:
        try:
            if not isinstance(scope, str) or str(uuid.UUID(scope)) != scope:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as error:
            raise PmtError("switch_state_unverifiable", f"{label} contains a wildcard or noncanonical scope", 3) from error
        scopes.add(scope)
    if not scopes:
        raise PmtError("switch_state_unverifiable", f"{label} contains no scopes", 3)
    return scopes


def _verify_hosted_no_claims(config_root, data_root, profile, *, hosted_reader=None):
    _check_pending_sidecars(data_root)
    original_credential = os.environ.get(ENV_NAME)
    try:
        from ..storage_config import probe_storage
        load_credential(config_root, os.environ)
        preflight = probe_storage(str(config_root))
        if (preflight.get("configured") is not True or preflight.get("mode") != "hosted"
                or preflight.get("actor") != profile["actor"]
                or preflight.get("device_id") != profile["device_id"]
                or preflight.get("namespace_id") != profile["namespace_id"]):
            raise PmtError("switch_state_unverifiable", "Authenticated Host identity differs from the saved profile", 3)
        auth_facts = preflight.get("host_preflight")
        current_scopes = _canonical_scopes(auth_facts.get("scopes") if isinstance(auth_facts, dict) else None,
                                           "Authenticated Host")
        metadata = client.read_client_metadata(config_root)
        if client.has_client_metadata(config_root) and metadata is None:
            raise PmtError("switch_state_unverifiable", "Saved client scope metadata is invalid", 3)
        scope_snapshot = (metadata or {}).get("host_scope_snapshot")
        if scope_snapshot is not None:
            if (not isinstance(scope_snapshot, dict) or scope_snapshot.get("schema_version") != 1
                    or scope_snapshot.get("device_id") != profile["device_id"]
                    or scope_snapshot.get("namespace_id") != profile["namespace_id"]
                    or scope_snapshot.get("actor") != profile["actor"]):
                raise PmtError("switch_state_unverifiable", "Saved Host scope identity is stale or invalid", 3)
            handoff_scopes = _canonical_scopes(scope_snapshot.get("handoff_scopes"), "Saved handoff")
            prior_scopes = _canonical_scopes(scope_snapshot.get("authenticated_scopes"), "Saved Host grants")
            stored_scopes = handoff_scopes | prior_scopes
            if not stored_scopes <= current_scopes:
                raise PmtError("switch_state_unverifiable", "Host grants changed since connect; reconnect before switching", 3)
        else:
            # Legacy profiles use only the fresh authenticated scope list; project
            # names/mappings are never treated as a complete grant list.
            handoff_scopes = current_scopes
        scopes_to_scan = sorted(current_scopes | (stored_scopes if scope_snapshot is not None else set()))
        store = session_id = normalize_request = None
        if hosted_reader is None:
            from ..http_store import HttpStore
            from ..service import normalize_request as normalize
            store = HttpStore(profile["endpoint"], profile["credential_env"], profile["device_id"],
                              profile["environment_id"], profile["namespace_id"], profile.get("ca_file"))
            session_id, normalize_request = "pmt-setup-" + profile["environment_id"], normalize
        for scope_id in scopes_to_scan:
            cursor = None
            for _page in range(1000):
                payload = {"limit": 200}
                if cursor:
                    payload["cursor"] = cursor
                if hosted_reader is not None:
                    result = hosted_reader(scope_id, payload)
                else:
                    request = normalize_request({"protocol_version": 1, "operation": "read_context",
                        "request_id": str(uuid.uuid4()), "actor": profile["actor"],
                        "session_id": session_id, "scope_id": scope_id, "payload": payload})
                    envelope, exit_code = store.execute(request)
                    if exit_code != 0 or not envelope.get("ok"):
                        code = (envelope.get("error") or {}).get("code", "remote_unavailable")
                        raise PmtError(code, "Host claim state could not be verified", exit_code or 3)
                    result = envelope["result"]
                for record in result.get("records", []):
                    if record.get("state") == "In Progress":
                        _fail("The Host reports an active claim; storage mode was not changed")
                cursor = result.get("next_cursor")
                if not cursor:
                    break
            else:
                raise PmtError("switch_state_unverifiable", "Host claim scan exceeded its page limit", 3)
        return {"schema_version": 1, "actor": profile["actor"],
                "device_id": profile["device_id"], "namespace_id": profile["namespace_id"],
                "handoff_scopes": sorted(handoff_scopes),
                "authenticated_scopes": sorted(current_scopes)}
    except PmtError as error:
        if error.code in {"switch_busy", "switch_state_unverifiable"}:
            raise
        raise PmtError("switch_state_unverifiable", "Current Host claim state could not be verified; local mode was not opened", 3) from error
    finally:
        if original_credential is None:
            os.environ.pop(ENV_NAME, None)
        else:
            os.environ[ENV_NAME] = original_credential


def _client_local_mappings(config_root, profile):
    metadata = client.read_client_metadata(config_root)
    if client.has_client_metadata(config_root) and metadata is None:
        raise PmtError("client_metadata_invalid", "Client metadata is invalid; switch was not applied", 3)
    if metadata and isinstance(metadata.get("local_workspace_mappings"), list):
        return metadata["local_workspace_mappings"]
    return list(profile.get("workspace_mappings", []))


def _restore_client_metadata(config_root, before, after):
    path = Path(config_root) / "client.json"
    with _config_lock(Path(config_root)):
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != after:
            return False
        if before is None:
            path.unlink(missing_ok=True)
        else:
            connect._atomic_file(path, before)
        return True


def _switch_to_hosted(config_root, data_root, profile, handoff_path, credential, export_path,
                      environ, configure):
    if not handoff_path:
        raise PmtError("handoff_invalid", "Hosted switch requires --handoff", 2)
    document = handoff.load_handoff(handoff_path)
    if document["device"]["credential"]["env"] != ENV_NAME:
        raise PmtError("handoff_invalid", "Handoff credential reference must be PMT_HOST_CREDENTIAL")
    explicit = environ.get(ENV_NAME)
    if credential is not None and explicit and credential != explicit:
        raise PmtError("credential_conflict", "CLI credential differs from PMT_HOST_CREDENTIAL; unset the variable or use the same credential")
    if credential is None and not explicit:
        if not has_credential_store(config_root):
            raise PmtError("credential_unavailable", "Provide a Host credential with --credential-file, --credential-stdin, or PMT_HOST_CREDENTIAL")
        load_credential(config_root, environ={})
    export_destination = _validate_export_path(export_path, data_root, config_root, profile)
    db_path = _local_preflight(config_root, data_root, profile)
    if export_destination is not None and db_path is None:
        raise PmtError("switch_export_unavailable", "Local database is not initialized; export was not created", 3)

    lock_root = Path(config_root)
    with connect.setup_lock(lock_root):
        current, current_hash = _read_profile(config_root)
        if current != profile:
            raise PmtError("storage_config_conflict", "Storage settings changed while switch was preparing", 3)
        claims_guard = (local_commands._state_lock(data_root, "easy-claims")
                        if Path(data_root).is_dir() else nullcontext())
        with claims_guard:
            db_path = _local_preflight(config_root, data_root, current)
            if export_destination is not None and db_path is None:
                raise PmtError("switch_export_unavailable", "Local database is not initialized; export was not created", 3)
            if db_path is None:
                return connect.connect(config_root, handoff_path, credential=credential, environ=environ,
                    configure=configure, _allow_local_switch=True, _setup_locked=True)
            original_hash = _file_sha(db_path)
            guard = None
            try:
                try:
                    guard = sqlite3.connect(str(db_path), timeout=0.25, isolation_level=None)
                    guard.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as error:
                    raise PmtError("switch_busy", "Local PMT database has an active writer; storage mode was not changed", 3) from error
                with closing(_readonly_connection(db_path)) as snapshot:
                    _assert_quiescent(snapshot, label="Source database")
                backup = _create_export(db_path, data_root, config_root, current, export_destination) if export_destination else None
                local_mappings = [{key: value for key, value in mapping.items()
                                   if key in {"repository_id", "project_id", "branch", "branch_key_sha256",
                                              "local_root", "relative_graph_path"}}
                                  for mapping in current.get("workspace_mappings", [])]
                result = connect.connect(config_root, handoff_path, credential=credential, environ=environ,
                    configure=configure, _allow_local_switch=True, _setup_locked=True,
                    _client_metadata_extra={"local_workspace_mappings": local_mappings})
                if _file_sha(db_path) != original_hash:
                    raise PmtError("switch_database_changed", "Local database bytes changed during switch", 4)
                result["export"] = backup
                result["mode"] = "hosted"
                return result
            finally:
                if guard is not None:
                    try:
                        guard.rollback()
                    finally:
                        guard.close()


def _switch_to_local(config_root, data_root, profile, *, hosted_reader=None):
    db_path = _local_preflight(config_root, data_root, profile)
    mappings = _client_local_mappings(config_root, profile)
    # Validate saved mappings before changing the mode; never invent IDs.
    from ..storage_config import _mappings
    mappings = _mappings(mappings)
    path = storage_path(config_root)
    old_storage = path.read_bytes()
    old_hash = _sha(old_storage)
    metadata = client.read_client_metadata(config_root)
    source = metadata["source"] if metadata else "connect"
    python_path = (metadata or {}).get("python_path") or os.path.realpath(os.sys.executable)

    with connect.setup_lock(config_root):
        current, current_hash = _read_profile(config_root)
        if current != profile or current_hash != old_hash:
            raise PmtError("storage_config_conflict", "Storage settings changed while switch was preparing", 3)
        claims_guard = (local_commands._state_lock(data_root, "easy-claims")
                        if Path(data_root).is_dir() else nullcontext())
        with claims_guard:
            _local_preflight(config_root, data_root, current)
            original_hash = _file_sha(db_path) if db_path is not None else None
            guard = None
            try:
                if db_path is not None:
                    try:
                        guard = sqlite3.connect(str(db_path), timeout=0.25, isolation_level=None)
                        guard.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as error:
                        raise PmtError("switch_busy", "Local PMT database has an active writer; storage mode was not changed", 3) from error
                    with closing(_readonly_connection(db_path)) as snapshot:
                        _assert_quiescent(snapshot, label="Source database")
                # Claims cannot start through this client while setup+claims guards
                # are held; repeat the Host scan immediately before publishing.
                scope_snapshot = _verify_hosted_no_claims(config_root, data_root, current,
                                                         hosted_reader=hosted_reader)
                _metadata_path, metadata_before, metadata_after = client.write_client_metadata_snapshot(
                    config_root, source=source, python_path=python_path, mode="local")
                try:
                    metadata_after = connect._merge_client_metadata_extra(
                        config_root, metadata_after, {"host_scope_snapshot": scope_snapshot})
                    result = configure_storage(str(config_root), {"mode": "local",
                        "expected_config_sha256": old_hash, "workspace_mappings": mappings})
                except Exception as error:
                    if not _restore_client_metadata(config_root, metadata_before, metadata_after):
                        raise PmtError("storage_config_conflict", "Switch failed and later client metadata was preserved") from error
                    raise
                if db_path is not None and _file_sha(db_path) != original_hash:
                    raise PmtError("switch_database_changed", "Local database bytes changed during switch", 4)
            finally:
                if guard is not None:
                    try:
                        guard.rollback()
                    finally:
                        guard.close()
    result["mode"] = "local"
    return result


def switch_storage(config_root, data_root, *, to, handoff_path=None, credential=None,
                   export_path=None, environ=None, configure=configure_storage, hosted_reader=None):
    """Explicit C09 switch; never constructs or migrates the original DB."""
    environ = os.environ if environ is None else environ
    config_root, data_root = Path(config_root).expanduser().resolve(), Path(data_root).expanduser().resolve()
    if to not in {"hosted", "local"}:
        raise PmtError("switch_mode_invalid", "Switch target must be hosted or local", 2)
    profile, _digest = _read_profile(config_root)
    if profile is None:
        raise PmtError("storage_profile_missing", "Create a local PMT profile before switching", 3)
    if to == profile["mode"]:
        return {"mode": profile["mode"], "unchanged": True}
    if to == "hosted":
        if profile["mode"] != "local":
            raise PmtError("switch_mode_invalid", "Only local profiles can use the local-to-hosted switch", 3)
        return _switch_to_hosted(config_root, data_root, profile, handoff_path, credential,
                                 export_path, environ, configure)
    if profile["mode"] != "hosted":
        raise PmtError("switch_mode_invalid", "Only hosted profiles can use the hosted-to-local switch", 3)
    if export_path:
        raise PmtError("switch_export_invalid", "--export applies only to --to hosted", 2)
    return _switch_to_local(config_root, data_root, profile, hosted_reader=hosted_reader)
