"""Sanitized Host backups, isolated restore checks, imports and owned pruning."""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..db import Database
from ..errors import PmtError
from ..host.auth import AuthRegistry
from ..host.transfer import _read_archive, _stage_entries
from ..migration import MigrationCoordinator
from ..util import canonical_json
from .config import (_process_lock, _publish_config_locked, config_path,
                     _path_has_reparse, load_config_snapshot, validate_config)
from .registry import _host
from .secrets import _set_windows_acl
from .serve import instance_available


class _AuthenticatedHostBoundary:
    """Minimal MigrationCoordinator adapter backed by real AuthRegistry checks."""

    def __init__(self, db, auth):
        self.db, self.auth = db, auth

    def principal(self, conn, headers, *, session=True):
        authorization = headers.get("authorization", "")
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise PmtError("unauthenticated", "A registered device credential is required", 3)
        return self.auth.authenticate(conn, authorization[7:], headers.get("x-pmt-device", ""),
                                      headers.get("x-pmt-namespace", ""),
                                      session_id=headers.get("x-pmt-session") if session else None,
                                      environment_id=headers.get("x-pmt-environment") if session else None)


def _issue_local_admin(auth, actor):
    issued = auth.issue_device(actor, ["*"], ["admin", "read", "write"])
    session_id, environment_id = str(uuid.uuid4()), str(uuid.uuid4())
    headers = {"authorization": "Bearer " + issued["credential"], "x-pmt-device": issued["device_id"],
               "x-pmt-namespace": auth.namespace_id, "x-pmt-session": session_id,
               "x-pmt-environment": environment_id}
    try:
        auth.register_session(issued["credential"], issued["device_id"], auth.namespace_id, session_id, environment_id)
    except Exception as error:
        try: auth.revoke_device(issued["device_id"], issued["revision"])
        except Exception as revoke_error:
            raise PmtError("temporary_admin_revoke_failed",
                           f"Temporary admin device {issued['device_id']} could not be revoked") from revoke_error
        raise error
    return issued, headers


def _revoke_local_admin(auth, issued):
    try:
        return auth.revoke_device(issued["device_id"], issued["revision"])
    except Exception as exc:
        raise PmtError("temporary_admin_revoke_failed",
                       f"Temporary admin device {issued['device_id']} could not be revoked; revoke it manually") from exc


def _backup_root(config):
    path = Path(config["paths"]["backup_dir"])
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
        from .serve import _runtime_path_safe
        if (not stat.S_ISDIR(info.st_mode) or path.is_symlink()
                or bool(getattr(info, "st_file_attributes", 0) & 0x400)
                or _path_has_reparse(path) or not _runtime_path_safe(path)
                or not os.access(path, os.R_OK | os.W_OK)):
            raise ValueError
        return resolved
    except (OSError, ValueError) as exc:
        raise PmtError("path_unsafe", "Configured backup directory is absent or unsafe") from exc


def _utc_bundle_name(bundle_id):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{bundle_id}"


def create_backup(config_root, *, apply=False):
    root = Path(config_root)
    if not apply:
        config, _ = load_config_snapshot(config_path(root))
        backup_root = _backup_root(config)
        return {"ok": True, "applied": False, "operation": "backup_create",
                "destination_parent": str(backup_root), "format": "pmt-migration-directory"}

    with _process_lock(root / ".host-config.lock"):
        config, _digest, db, auth = _host(root)
        destination_parent = _backup_root(config)
        data_root, config_dir = Path(config["paths"]["data_root"]).resolve(), root.resolve()
        if (destination_parent.is_relative_to(data_root) or data_root.is_relative_to(destination_parent)
                or destination_parent.is_relative_to(config_dir) or config_dir.is_relative_to(destination_parent)):
            raise PmtError("migration_destination_invalid", "Backup directory must be outside Host data and config roots")
        issued = None
        destination = None
        manifest = None
        failure = None
        try:
            issued, headers = _issue_local_admin(auth, "pmt-server-backup")
            bundle_id = str(uuid.uuid4())
            destination = destination_parent / _utc_bundle_name(bundle_id)
            manifest = MigrationCoordinator().create_host_backup(
                _AuthenticatedHostBoundary(db, auth), destination, headers, bundle_id=bundle_id
            )
        except Exception as exc:
            failure = exc
        cleanup_failure = None
        if issued is not None:
            try: _revoke_local_admin(auth, issued)
            except PmtError as exc: cleanup_failure = exc
        if cleanup_failure is not None:
            completed = destination is not None and destination.is_dir()
            extra = f"; completed bundle: {destination}" if completed else ""
            raise PmtError("backup_admin_cleanup_failed",
                           f"Temporary admin device {issued['device_id']} could not be revoked{extra}; revoke it manually") from cleanup_failure
        if failure is not None: raise failure
        return {"ok": True, "applied": True, "bundle_path": str(destination),
                "bundle_id": manifest["bundle_id"], "manifest_sha256": manifest["manifest_sha256"],
                "source_kind": manifest["source_kind"], "tables": manifest["tables"],
                "resource_count": len(manifest["resources"]), "excluded_history": manifest["excluded_history"]}


def _directory_bundle(path):
    source = Path(path)
    if source.is_symlink() or bool(getattr(source.lstat(), "st_file_attributes", 0) & 0x400):
        raise PmtError("migration_bundle_invalid", "Backup source cannot be a symlink or reparse point")
    if not source.is_dir(): return None
    return source.resolve(strict=True)


def _archive_to_directory(archive_path, staging_parent):
    path = Path(archive_path)
    try:
        info = path.lstat()
        if path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400) or not stat.S_ISREG(info.st_mode):
            raise PmtError("transfer_path_invalid", "Transfer archive must be a regular non-reparse file")
        if info.st_size <= 0 or info.st_size > 64 * 1024 * 1024:
            raise PmtError("transfer_archive_too_large", "Compressed bundle exceeds the 64 MiB limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024: raise PmtError("transfer_archive_too_large", "Compressed bundle exceeds the 64 MiB limit")
    except PmtError: raise
    except OSError as exc: raise PmtError("transfer_archive_invalid", "Transfer archive is unavailable") from exc
    _manifest, entries = _read_archive(raw)
    staged = Path(staging_parent) / ("unpacked-" + uuid.uuid4().hex)
    staged.mkdir(mode=0o700)
    _stage_entries(staged, entries)
    return staged


def _resolve_bundle(path, temporary_root):
    directory = _directory_bundle(path)
    if directory is not None: return directory, None
    return _archive_to_directory(path, temporary_root), "owned-unpack"


def _make_scratch(backup_root, prefix):
    try:
        scratch = Path(tempfile.mkdtemp(prefix=prefix, dir=backup_root))
        if os.name == "nt": _set_windows_acl(scratch, "current")
        else: os.chmod(scratch, 0o700)
        return scratch.resolve(strict=True), backup_root.resolve(strict=True)
    except OSError as exc:
        raise PmtError("host_io_error", "Could not create an isolated restore-check workspace") from exc


def _clean_scratch(scratch, parent, prefix):
    scratch = Path(scratch)
    if scratch.exists() and scratch.resolve(strict=True).parent == Path(parent).resolve(strict=True) and scratch.name.startswith(prefix):
        shutil.rmtree(scratch)


def restore_check(config_root, bundle_path):
    root = Path(config_root)
    config, _digest, _db, _auth = _host(root)
    parent = _backup_root(config)
    scratch, parent = _make_scratch(parent, ".pmt-restore-check-")
    issued = None
    error = None
    receipt = manifest = None
    try:
        bundle, _owned_unpack = _resolve_bundle(bundle_path, scratch)
        manifest = MigrationCoordinator().verify_backup(bundle)
        target_root = scratch / "target"
        target_config, target_data = target_root / "config", target_root / "data"
        target_db = Database(root=target_data, config_root=target_config)
        target_auth = AuthRegistry(target_db)
        issued, headers = _issue_local_admin(target_auth, "pmt-server-restore-check")
        receipt = MigrationCoordinator().restore_backup(_AuthenticatedHostBoundary(target_db, target_auth), bundle, headers)
    except Exception as exc:
        error = exc
    cleanup_error = None
    if issued is not None:
        try: _revoke_local_admin(target_auth, issued)
        except PmtError as exc: cleanup_error = exc
    try: _clean_scratch(scratch, parent, ".pmt-restore-check-")
    except OSError: cleanup_error = cleanup_error or PmtError("restore_check_cleanup_failed", "Temporary restore-check files could not be removed")
    if cleanup_error:
        raise PmtError("restore_check_cleanup_failed", "Temporary restore-check state or its admin device could not be cleaned") from cleanup_error
    if error: raise error
    return {"ok": True, "valid": True, "bundle_id": manifest["bundle_id"],
            "manifest_sha256": manifest["manifest_sha256"], "tables": manifest["tables"],
            "resource_count": len(manifest["resources"]), "receipt": receipt}


def _project_registry_entries(bundle, config):
    database = Path(bundle) / "transfer.sqlite3"
    try:
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=1)) as connection:
            rows = connection.execute("SELECT p.id,p.slug,r.id,r.kind FROM scopes p "
                                      "LEFT JOIN scopes r ON r.id=p.parent_id WHERE p.kind='project' ORDER BY p.slug,p.id").fetchall()
    except sqlite3.Error as exc:
        raise PmtError("migration_reference_invalid", "Imported project scope mapping could not be read") from exc
    names = {item["name"]: item["project_id"] for item in config["registry"]["projects"]}
    ids = {item["project_id"] for item in config["registry"]["projects"]}
    result, mapping = [], []
    for project_id, slug, repository_id, parent_kind in rows:
        if project_id in ids: continue
        if not isinstance(project_id, str) or not isinstance(slug, str):
            raise PmtError("migration_reference_invalid", "Imported project ID is not canonical")
        try:
            if str(uuid.UUID(project_id)) != project_id: raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise PmtError("migration_reference_invalid", "Imported project ID is not canonical") from exc
        base = "".join(char if char.isascii() and (char.isalnum() or char in "._-") else "-" for char in slug).strip(".-_")[:48] or "project"
        name = base
        if name in names and names[name] != project_id: name = f"{base}-{project_id[:8]}"
        suffix = 1
        while name in names and names[name] != project_id:
            suffix += 1; name = f"{base[:48]}-{project_id[:8]}-{suffix}"
        names[name], ids = project_id, ids | {project_id}
        result.append({"name": name, "project_id": project_id, "repositories": []})
        mapping.append({"project_id": project_id, "name": name,
                        "repository_scope_id": repository_id if parent_kind == "repository" else None,
                        "repository_metadata_required": parent_kind == "repository"})
    return result, mapping


def import_bundle(config_root, bundle_path, *, apply=False):
    root = Path(config_root)
    if not apply:
        config, _ = load_config_snapshot(config_path(root))
        parent = _backup_root(config)
        scratch, parent = _make_scratch(parent, ".pmt-import-plan-")
        try:
            bundle, _owned = _resolve_bundle(bundle_path, scratch)
            manifest = MigrationCoordinator().verify_backup(bundle)
            projects, mapping = _project_registry_entries(bundle, config)
            return {"ok": True, "applied": False, "operation": "import", "bundle_id": manifest["bundle_id"],
                    "manifest_sha256": manifest["manifest_sha256"], "new_projects": mapping,
                    "target_namespace_id": "existing Host namespace; unchanged"}
        finally: _clean_scratch(scratch, parent, ".pmt-import-plan-")

    with _process_lock(root / ".host-config.lock"):
        config, digest, db, auth = _host(root)
        data_root = Path(config["paths"]["data_root"])
        if not instance_available(data_root / ".pmt-server.lock"):
            raise PmtError("host_duplicate", "Stop the configured Host before importing business data", 3)
        backup_parent = _backup_root(config)
        scratch, backup_parent = _make_scratch(backup_parent, ".pmt-import-")
        issued = None
        manifest = receipt = mapping = None
        import_error = None
        try:
            bundle, _owned = _resolve_bundle(bundle_path, scratch)
            manifest = MigrationCoordinator().verify_backup(bundle)
            entries, mapping = _project_registry_entries(bundle, config)
            candidate = copy.deepcopy(config)
            candidate["registry"]["projects"].extend(entries)
            if entries:
                candidate["revision"] += 1
                validate_config(candidate)
            issued, headers = _issue_local_admin(auth, "pmt-server-import")
            receipt = MigrationCoordinator().restore_backup(_AuthenticatedHostBoundary(db, auth), bundle, headers)
        except Exception as exc:
            import_error = exc
        cleanup_error = None
        if issued is not None:
            try: _revoke_local_admin(auth, issued)
            except PmtError as exc: cleanup_error = exc
        try: _clean_scratch(scratch, backup_parent, ".pmt-import-")
        except OSError as exc: cleanup_error = cleanup_error or exc
        if import_error is not None:
            committed_receipt = _committed_import_receipt(db, manifest["bundle_id"]) if manifest is not None else None
            if committed_receipt is not None:
                # The coordinator may fail after its SQL commit (for example during
                # post-commit resource cleanup); continue with registry recovery.
                receipt = committed_receipt
                import_error = None
        if import_error is not None:
            if cleanup_error is not None:
                raise PmtError("import_cleanup_failed", f"Import failed; {cleanup_error}") from cleanup_error
            raise import_error

        registry_updated = not bool(entries)
        try:
            if entries:
                _publish_config_locked(root, candidate, digest)
                registry_updated = True
        except Exception as exc:
            return {"ok": False, "applied": True, "imported": True, "receipt": receipt,
                    "bundle_id": manifest["bundle_id"], "manifest_sha256": manifest["manifest_sha256"],
                    "new_projects": mapping, "registry_updated": registry_updated,
                    "error_code": exc.code if isinstance(exc, PmtError) else "registry_update_failed",
                    "guidance": "Host business import committed; rerun the same import command to replay safely and retry the registry CAS"}
        if cleanup_error:
            return {"ok": False, "applied": True, "imported": True, "receipt": receipt,
                    "bundle_id": manifest["bundle_id"], "manifest_sha256": manifest["manifest_sha256"],
                    "new_projects": mapping, "registry_updated": registry_updated,
                    "error_code": getattr(cleanup_error, "code", "import_cleanup_failed"),
                    "guidance": f"Temporary admin cleanup needs attention: {cleanup_error}"}
        return {"ok": True, "applied": True, "imported": True, "receipt": receipt,
                "bundle_id": manifest["bundle_id"], "manifest_sha256": manifest["manifest_sha256"],
                "new_projects": mapping, "registry_updated": True}


def _host_identity(db):
    try:
        with closing(sqlite3.connect(f"file:{db.path.as_posix()}?mode=ro", uri=True, timeout=1)) as connection:
            value = connection.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()
        if not value or not isinstance(value[0], str): raise ValueError
        return hashlib.sha256(value[0].encode("utf-8")).hexdigest()
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise PmtError("host_schema_unsupported", "Host database identity is unavailable") from exc


def _committed_import_receipt(db, bundle_id):
    key = "pmt_migration_import_v1:" + bundle_id + ":receipt"
    try:
        with closing(sqlite3.connect(f"file:{db.path.as_posix()}?mode=ro", uri=True, timeout=1)) as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        value = json.loads(row[0]) if row else None
        return value if isinstance(value, dict) else None
    except (sqlite3.Error, OSError, ValueError):
        return None


def prune_backups(config_root, *, keep=14, apply=False):
    if type(keep) is not int or not 1 <= keep <= 10000:
        raise PmtError("host_input_invalid", "--keep must be between 1 and 10000")
    root = Path(config_root)
    with _process_lock(root / ".host-config.lock"):
        return _prune_backups_locked(root, keep=keep, apply=apply)


def _prune_backups_locked(config_root, *, keep, apply):
    root = Path(config_root)
    with _process_lock(root / ".host-config.lock"):
        return _prune_backups_locked(root, keep=keep, apply=apply)


def _prune_backups_locked(config_root, *, keep, apply):
    config, _digest, db, _auth = _host(config_root)
    backup_root = _backup_root(config)
    identity = _host_identity(db)
    valid = []
    preserved = 0
    for entry in backup_root.iterdir():
        try:
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode) or entry.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                preserved += 1; continue
            resolved = entry.resolve(strict=True)
            if resolved.parent != backup_root: preserved += 1; continue
            manifest = MigrationCoordinator().verify_backup(resolved)
            if (manifest.get("source_kind") != "host" or manifest.get("tier") != "host-fixture-preparation"
                    or manifest.get("source_database_identity_sha256") != identity
                    or not re.fullmatch(r"\d{8}T\d{6}Z-" + re.escape(manifest.get("bundle_id", "")), entry.name)):
                preserved += 1; continue
            valid.append((manifest.get("created_at", ""), resolved, manifest))
        except (OSError, PmtError):
            preserved += 1
    valid.sort(key=lambda item: item[0], reverse=True)
    remove = valid[keep:]
    if not apply:
        return {"ok": True, "applied": False, "keep": keep,
                "verified_count": len(valid), "would_remove": [str(path) for _, path, _ in remove],
                "preserved_unverified_count": preserved}
    removed = []
    for _, path, _manifest in remove:
        if path.resolve(strict=True).parent != backup_root or path.is_symlink():
            raise PmtError("migration_destination_invalid", "Backup changed containment during prune")
        shutil.rmtree(path)
        removed.append(str(path))
    return {"ok": True, "applied": True, "keep": keep, "verified_count": len(valid),
            "removed": removed, "preserved_unverified_count": preserved}
