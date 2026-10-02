"""Authenticated local-fixture transport for versioned migration bundles.

The caller supplies bundle bytes and manifest identities, never a server path,
source URI, SQL, or credential. HTTP routes are intentionally registered by
the Host application owner, not this transport-independent service.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
import zlib
from contextlib import closing

from ..errors import PmtError
from ..migration import MIGRATION_SCHEMA, MigrationCoordinator
from ..resources import _reject_links
from ..util import canonical_json, new_id, utc_now
from .auth import identifier

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 2048
MAX_MANIFEST_BYTES = 1024 * 1024
TRANSFER_METADATA_HEADER = "X-PMT-Transfer-Metadata"
DOWNLOAD_REF_HEADER = "X-PMT-Download-Ref"
MANIFEST_SHA256_HEADER = "X-PMT-Manifest-SHA256"
BUNDLE_SHA256_HEADER = "X-PMT-Bundle-SHA256"
_HEX64 = frozenset("0123456789abcdef")
_RECEIPTS_DDL = """
CREATE TABLE IF NOT EXISTS host_transfer_receipts(
 direction TEXT NOT NULL CHECK(direction IN ('backup','import')),
 request_id TEXT NOT NULL,
 bundle_id TEXT NOT NULL,
 manifest_sha256 TEXT NOT NULL,
 archive_sha256 TEXT NOT NULL,
 size_bytes INTEGER NOT NULL,
 relative_path TEXT,
 actor TEXT NOT NULL,
 device_id TEXT NOT NULL,
 session_id TEXT NOT NULL,
 namespace_id TEXT NOT NULL,
 state TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(direction,request_id),
 UNIQUE(direction,bundle_id));
CREATE INDEX IF NOT EXISTS host_transfer_receipts_bundle_idx
 ON host_transfer_receipts(direction,bundle_id,manifest_sha256);
"""


def _fail(code, message, status=400, details=None, retryable=False):
    raise PmtError(code, message, status, retryable, details)


def _canonical_uuid(value, label):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise PmtError("transfer_input_invalid", f"{label} must be a canonical UUID") from exc
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _unlink_owned_temp(path: Path) -> None:
    for attempt in range(5):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.025 * (attempt + 1))


def _valid_sha(value):
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX64 for char in value)


def _entry_kind(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if (not isinstance(name, str) or not name or "\x00" in name or "\\" in name
            or name.startswith("/") or ":" in name or info.is_dir()):
        _fail("transfer_archive_path_invalid", "Archive contains an unsafe or non-file entry")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(part in {"", ".", ".."} for part in path.parts):
        _fail("transfer_archive_path_invalid", "Archive path is not canonical relative POSIX syntax")
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    windows_attributes = info.external_attr & 0xFFFF
    if (file_type not in {0, stat.S_IFREG} or info.flag_bits & 0x1
            or windows_attributes & (0x0400 | 0x0010 | 0x0040)):
        _fail("transfer_archive_entry_unsupported", "Archive symlinks, special files, and encrypted entries are unsupported")
    if name in {"manifest.json", "transfer.sqlite3"}:
        return name
    parts = path.parts
    if (len(parts) == 2 and parts[0] == "resources" and len(parts[1]) == 64
            and all(char in _HEX64 for char in parts[1])):
        return "resource"
    _fail("transfer_archive_entry_unexpected", "Archive contains an unexpected entry name")


def _read_archive(bundle: bytes) -> tuple[dict, dict[str, bytes]]:
    if not isinstance(bundle, bytes) or not bundle:
        _fail("transfer_archive_invalid", "Bundle body must be nonempty bytes")
    if len(bundle) > MAX_ARCHIVE_BYTES:
        _fail("transfer_archive_too_large", "Compressed bundle exceeds the 64 MiB limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise PmtError("transfer_archive_invalid", "Bundle is not a valid ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_FILES:
            _fail("transfer_archive_file_count", "Archive file count is outside the supported bound")
        by_name = {}
        kind_by_name = {}
        compressed_total = 0
        uncompressed_total = 0
        for info in infos:
            kind = _entry_kind(info)
            if info.filename in by_name:
                _fail("transfer_archive_duplicate_entry", "Archive contains a duplicate entry name")
            if info.file_size < 0 or info.compress_size < 0:
                _fail("transfer_archive_size_invalid", "Archive entry has an invalid size")
            if info.file_size > MAX_UNCOMPRESSED_BYTES or info.compress_size > MAX_ARCHIVE_BYTES:
                _fail("transfer_archive_size_invalid", "Archive entry exceeds the supported size")
            if info.file_size and not info.compress_size:
                _fail("transfer_archive_ratio_invalid", "Archive entry has an invalid compression ratio")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                _fail("transfer_archive_compression_unsupported", "Only stored or deflated ZIP entries are supported")
            compressed_total += info.compress_size
            uncompressed_total += info.file_size
            if compressed_total > MAX_ARCHIVE_BYTES or uncompressed_total > MAX_UNCOMPRESSED_BYTES:
                _fail("transfer_archive_too_large", "Archive total compressed or uncompressed size exceeds 64 MiB")
            if info.filename == "manifest.json" and info.file_size > MAX_MANIFEST_BYTES:
                _fail("transfer_manifest_too_large", "Bundle manifest exceeds its 1 MiB limit")
            by_name[info.filename] = info
            kind_by_name[info.filename] = kind
        if "manifest.json" not in by_name or "transfer.sqlite3" not in by_name:
            _fail("transfer_archive_incomplete", "Bundle requires manifest.json and transfer.sqlite3")
        try:
            contents = {name: archive.read(info) for name, info in by_name.items()}
        except (OSError, RuntimeError, zipfile.BadZipFile, zlib.error, NotImplementedError) as exc:
            raise PmtError("transfer_archive_corrupt", "Archive entry failed CRC or decompression validation", 400) from exc
    try:
        manifest = json.loads(contents["manifest.json"].decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PmtError("transfer_manifest_invalid", "Bundle manifest must be UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MIGRATION_SCHEMA:
        _fail("transfer_manifest_invalid", "Bundle manifest schema is unsupported")
    resource_entries = {name for name, kind in kind_by_name.items() if kind == "resource"}
    resource_list = manifest.get("resource_objects")
    if not isinstance(resource_list, list) or any(not isinstance(item, dict) for item in resource_list):
        _fail("transfer_manifest_invalid", "Manifest resource object list is invalid")
    resource_names = [item.get("bundle_path") for item in resource_list]
    if len(resource_names) != len(set(resource_names)):
        _fail("transfer_archive_duplicate_entry", "Manifest resource object list contains a duplicate path")
    expected_resources = set(resource_names)
    expected_names = {"manifest.json", "transfer.sqlite3"} | expected_resources
    if resource_entries != expected_resources or set(contents) != expected_names:
        _fail("transfer_archive_incomplete", "ZIP files do not match manifest resource paths")
    for name in resource_entries:
        digest = PurePosixPath(name).name
        if _sha256(contents[name]) != digest:
            _fail("transfer_resource_hash_mismatch", "A ZIP resource does not match its content-addressed filename")
    return manifest, contents


def _write_no_replace(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_links(path.parent)
    if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
        _fail("transfer_path_invalid", "Transfer archive target may not be a link", 500)
    if path.exists():
        if _file_hash(path) != (_sha256(raw), len(raw)):
            _fail("transfer_archive_conflict", "Server transfer path already contains different bytes", 409)
        return
    fd, raw_tmp = tempfile.mkstemp(prefix=".pmt-transfer-", dir=path.parent)
    temp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _reject_links(path.parent)
        try:
            os.link(temp_path, path)
        except FileExistsError:
            if _file_hash(path) != (_sha256(raw), len(raw)):
                _fail("transfer_archive_conflict", "Concurrent transfer publication has different bytes", 409)
        except OSError as exc:
            raise PmtError("transfer_atomic_publish_unsupported", "Transfer archive cannot be published without replacement", 503,
                           True, {"errno": getattr(exc, "errno", None)}) from exc
    finally:
        _unlink_owned_temp(temp_path)


class TransferService:
    """Authenticated bundle-byte transport for an existing HostApplication."""

    def __init__(self, application, transfer_root: str | os.PathLike):
        self.application = application
        expected_root = (Path(application.db.root) / "host-transfer-store").resolve()
        supplied = Path(transfer_root).expanduser().absolute()
        if supplied.resolve(strict=False) != expected_root:
            raise ValueError("transfer_root must be the Host-owned host-transfer-store directory")
        supplied.mkdir(parents=True, exist_ok=True)
        _reject_links(supplied)
        self.root = supplied
        with application.db.write() as conn:
            for statement in (item.strip() for item in _RECEIPTS_DDL.split(";")):
                if statement:
                    conn.execute(statement)
        self.coordinator = MigrationCoordinator()

    def _principal(self, headers, *permissions):
        with closing(self.application.db.connect()) as conn:
            principal = self.application.principal(conn, headers)
        for permission in permissions:
            principal.require(permission)
        if not principal.session_id:
            _fail("transfer_session_required", "A fresh registered Host session is required", 401)
        return principal

    @staticmethod
    def _owned(row, principal, namespace_id):
        if (row["actor"], row["device_id"], row["session_id"], row["namespace_id"]) != (
                principal.actor, principal.device_id, principal.session_id, namespace_id):
            _fail("transfer_owner_mismatch", "Transfer receipt belongs to another current owner/session", 403)

    def _receipt(self, direction, selector, value):
        with closing(self.application.db.connect()) as conn:
            return conn.execute(f"SELECT * FROM host_transfer_receipts WHERE direction=? AND {_quote_column(selector)}=?",
                                (direction, value)).fetchone()

    def _archive_path(self, bundle_id):
        _canonical_uuid(bundle_id, "bundle_id")
        path = self.root / (bundle_id + ".zip")
        if not path.resolve(strict=False).is_relative_to(self.root.resolve()):
            _fail("transfer_path_invalid", "Transfer archive must stay inside the Host-owned root", 500)
        _reject_links(path.parent)
        if path.is_symlink():
            _fail("transfer_path_invalid", "Transfer archive cannot be a symlink", 500)
        return path

    def _verify_archive_bytes(self, raw: bytes) -> dict:
        _manifest, entries = _read_archive(raw)
        stage_dir = self.root / (".verify-stage-" + new_id())
        stage_dir.mkdir(mode=0o700)
        try:
            _stage_entries(stage_dir, entries)
            return MigrationCoordinator().verify_backup(stage_dir)
        finally:
            if stage_dir.exists() and stage_dir.resolve().parent == self.root.resolve() \
                    and stage_dir.name.startswith(".verify-stage-"):
                shutil.rmtree(stage_dir)

    def _stored_archive_info(self, row, principal):
        self._owned(row, principal, self.application.auth.namespace_id)
        if row["state"] != "ready" or not isinstance(row["relative_path"], str):
            _fail("transfer_not_ready", "Transfer archive has not been durably published", 409, retryable=True)
        relative = PurePosixPath(row["relative_path"])
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts) or "\\" in row["relative_path"]:
            _fail("transfer_receipt_corrupt", "Stored transfer path is invalid", 500)
        path = self.root.joinpath(*relative.parts)
        if not path.resolve(strict=False).is_relative_to(self.root.resolve()) or path.is_symlink():
            _fail("transfer_receipt_corrupt", "Stored transfer path escaped its owner root", 500)
        archive_hash, archive_size = _file_hash(path)
        if (archive_hash, archive_size) != (row["archive_sha256"], row["size_bytes"]):
            _fail("transfer_archive_corrupt", "Server transfer archive failed hash/size verification", 500)
        manifest = self._verify_archive_bytes(path.read_bytes())
        if (manifest.get("bundle_id"), manifest.get("manifest_sha256")) != (
                row["bundle_id"], row["manifest_sha256"]):
            _fail("transfer_receipt_corrupt", "Server archive manifest differs from its receipt", 500)
        return path, manifest

    @staticmethod
    def _request_id(value):
        return _canonical_uuid(value, "request_id")

    def create_backup(self, request: dict, headers: dict) -> dict:
        if not isinstance(request, dict) or set(request) != {"request_id"}:
            _fail("transfer_input_invalid", "Backup request requires only request_id")
        request_id = self._request_id(request["request_id"])
        principal = self._principal(headers, "admin", "read", "write")
        existing = self._receipt("backup", "request_id", request_id)
        if existing:
            self._owned(existing, principal, self.application.auth.namespace_id)
            _path, manifest = self._stored_archive_info(existing, principal)
            return {"bundle_id": existing["bundle_id"], "manifest_sha256": existing["manifest_sha256"],
                "bundle_sha256": existing["archive_sha256"], "download_ref": existing["bundle_id"],
                "size_bytes": existing["size_bytes"], "state": "replayed",
                "source_kind": manifest.get("source_kind")}

        with closing(self.application.db.connect()) as conn:
            artifact_bytes = int(conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM artifacts WHERE state='ready'").fetchone()[0])
            page_bytes = (int(conn.execute("PRAGMA page_count").fetchone()[0])
                          * int(conn.execute("PRAGMA page_size").fetchone()[0]))
            if artifact_bytes + page_bytes > MAX_UNCOMPRESSED_BYTES:
                _fail("transfer_archive_too_large", "Host business data exceeds the 64 MiB export bound", 413,
                      {"artifact_bytes": artifact_bytes, "sqlite_page_bytes": page_bytes})

        bundle_id = str(uuid.uuid5(uuid.UUID(self.application.auth.namespace_id),
                                   "pmt-host-backup:" + request_id + ":" + principal.device_id + ":" + principal.session_id))
        archive_path = self._archive_path(bundle_id)
        if archive_path.exists():
            raw = archive_path.read_bytes()
            manifest = self._verify_archive_bytes(raw)
            if manifest.get("bundle_id") != bundle_id:
                _fail("transfer_receipt_corrupt", "Unregistered server archive has a different bundle ID", 500)
            archive_hash, archive_size = _sha256(raw), len(raw)
        else:
            # Bundle staging is outside the live database tree. The final ZIP alone
            # resides under the fixed Host-owned host-transfer-store path.
            stage_parent = Path(tempfile.mkdtemp(prefix="pmt-host-transfer-export-"))
            stage_dir = stage_parent / "bundle"
            try:
                manifest = self.coordinator.create_host_backup(self.application, stage_dir, headers,
                                                               bundle_id=bundle_id)
                if manifest.get("bundle_id") != bundle_id:
                    _fail("transfer_manifest_invalid", "Host backup coordinator changed the reserved bundle ID", 500)
                raw = _pack_bundle(stage_dir, manifest)
                archive_hash, archive_size = _sha256(raw), len(raw)
                try:
                    _write_no_replace(archive_path, raw)
                except PmtError as exc:
                    if exc.code != "transfer_archive_conflict" or not archive_path.exists():
                        raise
                    raw = archive_path.read_bytes()
                    manifest = self._verify_archive_bytes(raw)
                    if manifest.get("bundle_id") != bundle_id:
                        raise
                    archive_hash, archive_size = _sha256(raw), len(raw)
            finally:
                if stage_parent.exists() and stage_parent.name.startswith("pmt-host-transfer-export-"):
                    shutil.rmtree(stage_parent)
        with self.application.db.write() as conn:
            current = self.application.principal(conn, headers)
            current.require("admin")
            current.require("write")
            if (current.actor, current.device_id, current.session_id) != (
                    principal.actor, principal.device_id, principal.session_id):
                _fail("transfer_owner_mismatch", "Host backup owner changed during generation", 403)
            prior = conn.execute("SELECT * FROM host_transfer_receipts WHERE direction='backup' AND request_id=?",
                                 (request_id,)).fetchone()
            if prior:
                self._owned(prior, current, self.application.auth.namespace_id)
                if (prior["bundle_id"], prior["manifest_sha256"], prior["archive_sha256"]) != (
                        bundle_id, manifest["manifest_sha256"], archive_hash):
                    _fail("transfer_request_conflict", "Backup request ID maps to different bundle bytes", 409)
            else:
                conn.execute("INSERT INTO host_transfer_receipts(direction,request_id,bundle_id,manifest_sha256,archive_sha256,size_bytes,relative_path,actor,device_id,session_id,namespace_id,state,created_at,updated_at) "
                    "VALUES('backup',?,?,?,?,?,?,?,?,?,?, 'ready',?,?)",
                    (request_id, bundle_id, manifest["manifest_sha256"], archive_hash, archive_size,
                     archive_path.name, current.actor, current.device_id, current.session_id,
                     self.application.auth.namespace_id, utc_now(), utc_now()))
        return {"bundle_id": bundle_id, "manifest_sha256": manifest["manifest_sha256"],
            "bundle_sha256": archive_hash, "download_ref": bundle_id,
            "size_bytes": archive_size, "state": "ready", "source_kind": manifest.get("source_kind")}

    def download_bytes(self, download_ref: str, headers: dict) -> dict:
        bundle_id = _canonical_uuid(download_ref, "download_ref")
        principal = self._principal(headers, "admin", "read")
        row = self._receipt("backup", "bundle_id", bundle_id)
        if not row:
            _fail("transfer_not_found", "Transfer download ref is unavailable", 404)
        path, manifest = self._stored_archive_info(row, principal)
        raw = path.read_bytes()
        return {"content": raw, "bundle_id": bundle_id, "download_ref": bundle_id,
            "manifest_sha256": row["manifest_sha256"], "bundle_sha256": row["archive_sha256"],
            "size_bytes": len(raw), "source_kind": manifest.get("source_kind")}

    def import_bytes(self, metadata: dict, bundle_bytes: bytes, headers: dict) -> dict:
        if not isinstance(metadata, dict) or set(metadata) != {"bundle_id", "manifest_sha256"}:
            _fail("transfer_metadata_invalid", "Transfer metadata requires bundle_id and manifest_sha256")
        bundle_id = _canonical_uuid(metadata["bundle_id"], "bundle_id")
        if not _valid_sha(metadata["manifest_sha256"]):
            _fail("transfer_metadata_invalid", "manifest_sha256 must be a lowercase SHA-256 digest")
        principal = self._principal(headers, "admin", "write")
        manifest, entries = _read_archive(bundle_bytes)
        if (manifest.get("bundle_id"), manifest.get("manifest_sha256")) != (
                bundle_id, metadata["manifest_sha256"]):
            _fail("transfer_manifest_conflict", "Transfer headers differ from the embedded bundle manifest", 409)
        archive_sha = _sha256(bundle_bytes)
        request_id = str(uuid.uuid5(uuid.UUID(bundle_id), "pmt-transfer-import:" + metadata["manifest_sha256"]))
        existing = self._receipt("import", "bundle_id", bundle_id)
        if existing:
            self._owned(existing, principal, self.application.auth.namespace_id)
            if (existing["manifest_sha256"], existing["archive_sha256"]) != (
                    metadata["manifest_sha256"], archive_sha):
                _fail("transfer_bundle_conflict", "Bundle ID was imported with different manifest or body bytes", 409)

        stage_dir = self.root / (".import-stage-" + new_id())
        stage_dir.mkdir(mode=0o700)
        try:
            _stage_entries(stage_dir, entries)
            checked = self.coordinator.verify_backup(stage_dir)
            if checked["manifest_sha256"] != metadata["manifest_sha256"]:
                _fail("transfer_manifest_conflict", "Staged bundle manifest hash changed", 409)
            receipt = self.coordinator.restore_backup(self.application, stage_dir, headers)
            with self.application.db.write() as conn:
                current = self.application.principal(conn, headers)
                current.require("admin")
                current.require("write")
                if (current.actor, current.device_id, current.session_id) != (
                        principal.actor, principal.device_id, principal.session_id):
                    _fail("transfer_owner_mismatch", "Host import owner changed during restoration", 403)
                prior = conn.execute("SELECT * FROM host_transfer_receipts WHERE direction='import' AND bundle_id=?",
                                     (bundle_id,)).fetchone()
                if prior:
                    self._owned(prior, current, self.application.auth.namespace_id)
                    if (prior["manifest_sha256"], prior["archive_sha256"]) != (
                            metadata["manifest_sha256"], archive_sha):
                        _fail("transfer_bundle_conflict", "Bundle receipt changed during import", 409)
                else:
                    conn.execute("INSERT INTO host_transfer_receipts(direction,request_id,bundle_id,manifest_sha256,archive_sha256,size_bytes,relative_path,actor,device_id,session_id,namespace_id,state,created_at,updated_at) "
                        "VALUES('import',?,?,?,?,?,NULL,?,?,?,?,'imported',?,?)",
                        (request_id, bundle_id, metadata["manifest_sha256"], archive_sha,
                         len(bundle_bytes), current.actor, current.device_id, current.session_id,
                         self.application.auth.namespace_id, utc_now(), utc_now()))
            return {**receipt, "bundle_id": bundle_id, "manifest_sha256": metadata["manifest_sha256"],
                    "bundle_sha256": archive_sha, "state": "imported" if receipt.get("state") != "replayed" else "replayed"}
        finally:
            if stage_dir.exists() and stage_dir.resolve().parent == self.root.resolve() \
                    and stage_dir.name.startswith(".import-stage-"):
                shutil.rmtree(stage_dir)


def _pack_bundle(bundle_dir: Path, manifest: dict) -> bytes:
    root = Path(bundle_dir).resolve(strict=True)
    paths = {"manifest.json": root / "manifest.json", "transfer.sqlite3": root / "transfer.sqlite3"}
    for item in manifest["resource_objects"]:
        paths[item["bundle_path"]] = root / item["bundle_path"]
    if len(paths) > MAX_ARCHIVE_FILES:
        _fail("transfer_archive_file_count", "Bundle has more files than the archive limit", 413)
    total_size = 0
    for path in paths.values():
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            _fail("transfer_bundle_path_invalid", "Bundle source file escaped its owned staging root", 500)
        total_size += path.stat().st_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            _fail("transfer_archive_too_large", "Uncompressed bundle exceeds 64 MiB", 413)
    verified = MigrationCoordinator().verify_backup(root)
    if verified["manifest_sha256"] != manifest.get("manifest_sha256"):
        _fail("transfer_manifest_conflict", "Bundle changed before ZIP packaging", 409)
    total = 0
    contents = []
    for name in sorted(paths):
        path = paths[name]
        if not path.resolve(strict=True).is_relative_to(root) or path.is_symlink():
            _fail("transfer_bundle_path_invalid", "Bundle source file escaped its owned staging root", 500)
        raw = path.read_bytes()
        total += len(raw)
        if total > MAX_UNCOMPRESSED_BYTES:
            _fail("transfer_archive_too_large", "Uncompressed bundle exceeds 64 MiB", 413)
        contents.append((name, raw))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, raw in contents:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, raw)
    result = output.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        _fail("transfer_archive_too_large", "Compressed bundle exceeds 64 MiB", 413)
    return result


def _stage_entries(stage_dir: Path, entries: dict[str, bytes]) -> None:
    root = stage_dir.resolve(strict=True)
    for name in sorted(entries):
        path = PurePosixPath(name)
        target = root.joinpath(*path.parts)
        if not target.resolve(strict=False).is_relative_to(root):
            _fail("transfer_archive_path_invalid", "Staged archive path escaped its server-owned root")
        target.parent.mkdir(parents=True, exist_ok=True)
        _reject_links(target.parent)
        with target.open("xb") as stream:
            stream.write(entries[name])
            stream.flush()
            os.fsync(stream.fileno())


def _quote_column(value: str) -> str:
    if value not in {"request_id", "bundle_id"}:
        raise ValueError("unexpected transfer receipt selector")
    return '"' + value + '"'
