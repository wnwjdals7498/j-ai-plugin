# F13 migration preparation — 2026-10-02

Status: **local fixture preparation only**. This is not an HTTP operation, Host acceptance result, primary switch, or authorization to move a user database.

`pmt.migration.MigrationCoordinator` exposes `create_backup(source_db, destination, workspace_mappings)`, `verify_backup(bundle_path)`, and `stage_import(target_host, bundle_path, target_headers)` / `restore_backup(...)`. `verify` checks a bundle; `stage_import` imports into the supplied empty target fixture namespace, and repeating it after an interrupted response replays the committed receipt. `switch_primary` deliberately returns `migration_primary_switch_unavailable`; an F12 verified config receipt and main-owned change are still required.

The export checks core 0.3.0, SQLite schema 4, graph schema 1, and a clean Git branch/commit for each supplied workspace mapping. `workspace_mappings` has exact entries `{repository_id, local_workspace, branch}`. The branch is checked against local Git, then represented as `pmt://<repository UUID>/<SHA-256(branch key)>`; physical workspace paths are not written into the bundle. Missing mappings become `unknown-workspace:<hash>` and mark project baselines as not ready to resume.

The source writer barrier is temporary and always released. Export refuses active or queued runs, scope locks, claims, file jobs, unresolved operation/phase3/Host resource journals, pending outbox work, active Host claims, and changing local runner-spool inventory. A consistent SQLite backup API snapshot is held in memory, then only sanitized rows and content-addressed artifact bytes are written to the bundle. Existing terminal runner spool files remain at the source and are represented only by file-count/size/inventory hashes.

The transfer preserves IDs and relationships for scopes, records, events, plans, Step specifications, execution jobs/runs, project baselines, verifications, artifacts, and artifact references. Resource IDs and bytes retain their SHA-256. Historical verification rows are marked `migrated_stale` and use a nonmatching migration environment identity. Execution handles are removed; session ownership is replaced by a non-live migration marker; route agent/model/mode provenance remains while command/argv/environment/PID/callback data is redacted. Request IDs are recorded as history only; request responses and replay cache rows are excluded. Local scope paths, profile/meta, routing policy, claims, locks, spools, execution journals/outbox, Host devices/sessions/credentials/namespace, Host resource journals/requests, and derived F5/F8/F9/index objects are not imported.

Restore requires an authenticated target admin with write permission and an empty business namespace. Existing target Host namespace, devices, credentials, and keys remain in place. Verified content-addressed blobs are published without replacement before one short SQLite transaction copies whitelisted rows, rebinds Host resource metadata to the current importer, checks foreign keys/IDs/resource hashes, and records a manifest marker. A crash before SQL publication leaves only hash-checked unreferenced blobs; a crash after commit is replayed from the same marker. No database file swap occurs. Derived state is invalidated; no graph reindex, remote endpoint, or F12 local mapping/configuration is performed here.

## Verification

Command: `.venv\Scripts\python.exe -m pytest tests/test_phase3_migration.py -q --tb=short --basetemp=.pmt-test/p3-migration-recheck`

Exit code: **0** — **13 passed** in 28.05 seconds.

The fixture suite covers preserved scope/record/run/resource IDs and resource bytes; target auth/namespace preservation; sanitized route/session/command/claim-token data; request replay exclusion; clean Git-to-canonical workspace mapping and unknown mapping behavior; active run/claim/incomplete journal/pending outbox quiescence; nonempty, duplicate-ID, and active-claim target refusal; target/source schema mismatch; corrupt manifest/resource rejection; partial resource publish retry; post-commit replay; source spool preservation; and refusal to switch primary.

`HostResourceStore.read` verified an imported evidence resource against the target fixture. The tests used temporary SQLite databases, a temporary local Git repository, and local resource files only. No production/user database, external service, provider/model call, Host HTTP endpoint, primary switch, or live workspace mapping was used.
