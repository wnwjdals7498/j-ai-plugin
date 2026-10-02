from __future__ import annotations

import io
import json
import stat
import uuid
import zipfile
from contextlib import closing

import pytest

from pmt.errors import PmtError
from pmt.host.transfer import (BUNDLE_SHA256_HEADER, DOWNLOAD_REF_HEADER,
    MANIFEST_SHA256_HEADER, MAX_UNCOMPRESSED_BYTES, TransferService, _pack_bundle)
from pmt.migration import MigrationCoordinator
from pmt.util import new_id
from test_phase3_migration import _source_case, _target_host


def _zip(entries, compression=zipfile.ZIP_STORED, attrs=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression, allowZip64=False) as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = compression
            if attrs and name in attrs:
                info.external_attr = attrs[name]
            archive.writestr(info, content)
    return output.getvalue()


def _local_bundle(tmp_path):
    source = _source_case(tmp_path)
    bundle_path = tmp_path / "local-verified-bundle"
    manifest = MigrationCoordinator().create_backup(source["db"], bundle_path, [source["mapping"]])
    return source, bundle_path, manifest, _pack_bundle(bundle_path, manifest)


def test_local_bytes_import_then_host_backup_download_and_isolated_restore(tmp_path):
    source, bundle_path, manifest, bundle_bytes = _local_bundle(tmp_path)
    target_db, target, headers, target_device = _target_host(tmp_path)
    namespace = target.auth.namespace_id
    service = TransferService(target, target_db.root / "host-transfer-store")

    imported = service.import_bytes({"bundle_id": manifest["bundle_id"],
        "manifest_sha256": manifest["manifest_sha256"]}, bundle_bytes, headers)
    assert imported["state"] == "imported"
    replay = service.import_bytes({"bundle_id": manifest["bundle_id"],
        "manifest_sha256": manifest["manifest_sha256"]}, bundle_bytes, headers)
    assert replay["state"] == "replayed"

    backup = service.create_backup({"request_id": new_id()}, headers)
    assert backup["state"] == "ready"
    assert backup["source_kind"] == "host"
    assert backup["download_ref"] == backup["bundle_id"]
    downloaded = service.download_bytes(backup["download_ref"], headers)
    assert downloaded["bundle_sha256"] == backup["bundle_sha256"]
    assert downloaded["manifest_sha256"] == backup["manifest_sha256"]
    host_manifest, _ = __import__("pmt.host.transfer", fromlist=["_read_archive"])._read_archive(downloaded["content"])
    assert host_manifest["source_kind"] == "host"
    assert host_manifest["workspace_source_status"] == "canonical_refs_preserved_checkout_not_inspected"
    assert host_manifest["portable_workspace_mappings"] == []
    assert host_manifest["baseline_mapping_status"][0]["ready_for_resume"] is False

    restored_db, restored, restored_headers, restored_device = _target_host(tmp_path / "isolated")
    restore_service = TransferService(restored, restored_db.root / "host-transfer-store")
    restored_result = restore_service.import_bytes({"bundle_id": downloaded["bundle_id"],
        "manifest_sha256": downloaded["manifest_sha256"]}, downloaded["content"], restored_headers)
    assert restored_result["state"] == "imported"
    assert restored.auth.namespace_id != namespace
    with closing(restored_db.connect()) as conn:
        assert conn.execute("SELECT 1 FROM host_devices WHERE id=?", (restored_device,)).fetchone()
        assert conn.execute("SELECT 1 FROM records WHERE id=?", (source["step_id"],)).fetchone()
        assert conn.execute("SELECT 1 FROM artifacts WHERE id=?", (source["evidence_id"],)).fetchone()


def test_backup_download_replay_requires_current_same_owner_session(tmp_path):
    _source, _bundle_path, _manifest, _bundle_bytes = _local_bundle(tmp_path)
    # This Host fixture is independently empty; export works without copying local business data.
    host_db, host, headers, _device = _target_host(tmp_path / "host")
    service = TransferService(host, host_db.root / "host-transfer-store")
    request_id = new_id()
    first = service.create_backup({"request_id": request_id}, headers)
    replay = service.create_backup({"request_id": request_id}, headers)
    assert replay["state"] == "replayed"
    assert replay["bundle_sha256"] == first["bundle_sha256"]

    other = host.auth.issue_device("main", ["*"], ["read", "write", "admin"])
    other_headers = {"authorization": "Bearer " + other["credential"],
        "x-pmt-device": other["device_id"], "x-pmt-environment": new_id(),
        "x-pmt-namespace": host.auth.namespace_id, "x-pmt-session": "different-transfer-session"}
    host.register_session(other_headers, {"session_id": other_headers["x-pmt-session"],
        "environment_id": other_headers["x-pmt-environment"]})
    with pytest.raises(PmtError) as caught:
        service.download_bytes(first["download_ref"], other_headers)
    assert caught.value.code == "transfer_owner_mismatch"


def test_archive_rejects_slip_duplicate_reparse_and_size_excess(tmp_path, monkeypatch):
    source, bundle_path, manifest, raw = _local_bundle(tmp_path)
    host_db, host, headers, _device = _target_host(tmp_path / "host")
    service = TransferService(host, host_db.root / "host-transfer-store")
    metadata = {"bundle_id": manifest["bundle_id"], "manifest_sha256": manifest["manifest_sha256"]}
    with pytest.raises(PmtError) as caught:
        service.import_bytes(metadata, _zip([("../manifest.json", b"{}")]), headers)
    assert caught.value.code == "transfer_archive_path_invalid"

    manifest_bytes = (bundle_path / "manifest.json").read_bytes()
    with pytest.raises(PmtError) as caught:
        service.import_bytes(metadata, _zip([("manifest.json", manifest_bytes),
            ("manifest.json", manifest_bytes), ("transfer.sqlite3", (bundle_path / "transfer.sqlite3").read_bytes())]), headers)
    assert caught.value.code == "transfer_archive_duplicate_entry"

    symlink_zip = _zip([("manifest.json", manifest_bytes),
        ("transfer.sqlite3", (bundle_path / "transfer.sqlite3").read_bytes())],
        attrs={"transfer.sqlite3": (stat.S_IFLNK | 0o777) << 16})
    with pytest.raises(PmtError) as caught:
        service.import_bytes(metadata, symlink_zip, headers)
    assert caught.value.code == "transfer_archive_entry_unsupported"

    large = _zip([("manifest.json", b"M" * 700), ("transfer.sqlite3", b"A" * 400)])
    monkeypatch.setattr("pmt.host.transfer.MAX_UNCOMPRESSED_BYTES", 1024)
    with pytest.raises(PmtError) as caught:
        service.import_bytes(metadata, large, headers)
    assert caught.value.code == "transfer_archive_too_large"
    assert isinstance(raw, bytes)


def test_same_bundle_id_with_different_zip_body_is_rejected(tmp_path):
    source, bundle_path, manifest, bundle_bytes = _local_bundle(tmp_path)
    host_db, host, headers, _device = _target_host(tmp_path / "host")
    service = TransferService(host, host_db.root / "host-transfer-store")
    metadata = {"bundle_id": manifest["bundle_id"], "manifest_sha256": manifest["manifest_sha256"]}
    imported = service.import_bytes(metadata, bundle_bytes, headers)
    assert imported["state"] == "imported"
    names = [("manifest.json", (bundle_path / "manifest.json").read_bytes()),
             ("transfer.sqlite3", (bundle_path / "transfer.sqlite3").read_bytes())]
    names.extend((item["bundle_path"], (bundle_path / item["bundle_path"]).read_bytes())
                 for item in manifest["resource_objects"])
    alternate = _zip(names, compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(PmtError) as caught:
        service.import_bytes(metadata, alternate, headers)
    assert caught.value.code == "transfer_bundle_conflict"
