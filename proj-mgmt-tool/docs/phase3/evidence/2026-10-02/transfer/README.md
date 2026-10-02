# F13 transfer evidence — 2026-10-02

Status: **transport-independent fixtures and isolated loopback HTTPS transfer passed**. This is not a production migration or primary switch.

`pmt.host.transfer.TransferService` accepts a verified Host application and its fixed `host-transfer-store` directory. Its public methods are `import_bytes({bundle_id, manifest_sha256}, zip_bytes, headers)`, `create_backup({request_id}, headers)`, and `download_bytes(download_ref, headers)`. Wire inputs contain bundle bytes and hashes only; callers cannot supply server paths or source URIs. Backup/download results expose the bundle, manifest, and download-reference hashes needed by HTTP response headers.

The ZIP uses only `manifest.json`, `transfer.sqlite3`, and content-addressed `resources/<sha256>` entries. It rejects unsafe or noncanonical names, duplicate entries, links/special files, encryption, unexpected members, corrupt hashes, and archives above the 64 MiB compressed or expanded limits. Host backup preserves canonical workspace references as unverified and does not inspect Git. Re-import uses the migration coordinator's existing marker and row/hash checks for idempotent replay.

Command: `.venv\Scripts\python.exe -m pytest tests/test_phase3_transfer.py -q --tb=short --basetemp=.pmt-test/p3-transfer-recheck`

Exit code: **0** — **4 passed** in 13.78 seconds. The lone warning is from the test constructing a ZIP with an intentional duplicate member; the service rejects it as expected.

The direct fixture exercised local verified bytes → Host import → Host backup/download → isolated Host restore, same-body replay, owner/session rejection, archive path/duplicate/link/size rejection, and same-ID different-body conflict. No external service, provider/model API, production Host, user database, or primary switch was used.

Actual route command: `.venv\Scripts\python.exe -m pytest tests/test_phase3_transfer_http.py -q --tb=short --basetemp=.pmt-test/p3-transfer-https`

Exit code: **0** — **1 passed** in 7.28 seconds. This started two isolated Uvicorn HTTPS Hosts using fixture databases and ephemeral local certificates. A verified local bundle was imported and replayed on the first Host, exported through backup and binary download with all three hash/reference headers checked, then imported into the empty second Host. The test did not use a production Host or primary switch.
