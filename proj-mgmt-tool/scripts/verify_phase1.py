#!/usr/bin/env python3
"""Run the full checked-in pytest suite and report phase-1 evidence coverage.

Native installation results are accepted only from an explicit parent-authored
JSON evidence file; plugin/module presence is never treated as installation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

MAPPING_VERSION = 4
PRODUCTS = ("codex", "claude", "opencode")
G1_IDS = (
    "DATA-01", "DATA-02", "DATA-03", "TX-01", "IDEM-01", "IDEM-02",
    "EVENT-01", "EVENT-02", "REV-01", "CLAIM-01", "CLAIM-02", "CLAIM-03",
    "DONE-01", "DONE-02", "DECISION-01", "READ-01", "READ-02",
    "VERIFY-01", "VERIFY-02", "VERIFY-03", "RESOURCE-01", "RESOURCE-02",
    "RESOURCE-03", "BACKUP-01", "BACKUP-02", "CLI-01", "CLI-02", "CLI-03",
    "RECOVER-01", "STOP-01", "HOOK-01", "HOOK-02", "HOOK-03", "LOG-01",
    "LOG-02", "LOG-03",
)
G2_IDS = ("PKG-01",)
INSTALL_IDS = tuple(f"INSTALL-0{number}:{product}" for product in PRODUCTS for number in range(1, 6))
REQUIRED_IDS = G1_IDS + G2_IDS + INSTALL_IDS

# Module layer assignment is explicit. Coverage requires a second, exact node map below.
MODULE_LAYERS = {
    "tests/test_acceptance.py": "core_unit",
    "tests/test_backup.py": "core_unit",
    "tests/test_cli_integration.py": "core_process",
    "tests/test_end_to_end.py": "core_process",
    "tests/test_acceptance_matrix.py": "core_unit",
    "tests/test_final_guards.py": "core_unit",
    "tests/test_fixture_contract.py": "core_unit",
    "tests/test_foundation.py": "core_unit",
    "tests/test_hooks.py": "hook_fixture",
    "tests/test_hook_acceptance.py": "hook_fixture",
    "tests/test_lifecycle.py": "core_unit",
    "tests/test_lifecycle_extensions.py": "core_unit",
    "tests/test_packaging.py": "package",
    "tests/test_queries.py": "core_unit",
    "tests/test_resources.py": "core_unit",
    "tests/test_verification.py": "core_unit",
}


def _c(test_id: str, explanation: str, complete: bool = True, affects_result: bool = True) -> dict[str, Any]:
    return {"test_id": test_id, "complete": complete, "affects_result": affects_result, "coverage": explanation}


# Checked-in map: module path + exact pytest function node -> observed contract IDs.
# Partial entries are visible evidence but can never produce a phase-test pass.
TEST_NODE_MAP: dict[str, dict[str, Any]] = {
    "tests/test_acceptance.py::test_data_02_v1_migrates_and_future_schema_is_read_guarded": {
        "covers": [_c("DATA-02", "v1 request-table migration and future-schema refusal; migration failure rollback is not covered", False)],
    },
    "tests/test_acceptance.py::test_tx_01_event_ledger_failure_rolls_back_record": {
        "covers": [_c("TX-01", "injected event-ledger failure leaves record, event, and request state rolled back"),
                   _c("LOG-03", "business event-ledger write failure rolls the state transition back")],
    },
    "tests/test_acceptance.py::test_log_02_diagnostic_failure_keeps_committed_response_and_warning": {
        "covers": [_c("LOG-02", "diagnostic failure after commit preserves replayable success and warning")],
    },
    "tests/test_acceptance.py::test_log_03_request_ledger_failure_rolls_back_record_and_event": {
        "covers": [_c("LOG-03", "request-ledger failure rolls back record and event")],
    },
    "tests/test_acceptance.py::test_claim_03_recovery_replaces_owner_and_rejects_old_token": {
        "covers": [_c("CLAIM-03", "explicit recovery replaces ownership and rejects old token")],
    },
    "tests/test_acceptance.py::test_event_02_repeated_prompt_ids_and_reverse_stop_never_finish": {
        "covers": [_c("EVENT-02", "distinct prompt IDs survive reverse Stop arrival without Done")],
    },
    "tests/test_acceptance.py::test_decision_01_explicit_choice_supersession_and_stale_rejection": {
        "covers": [_c("DECISION-01", "explicit choice/supersession/stale revision; paired delegated-choice scenario is in the acceptance matrix")],
    },
    "tests/test_acceptance.py::test_recover_01_child_exit_boundary_and_request_replay": {
        "layer": "core_process",
        "expected_cases": 2,
        "covers": [_c("RECOVER-01", "independent child exits before and after commit, then same request replay")],
    },
    "tests/test_acceptance_matrix.py::test_data01_cli_setup_is_stable_across_processes_and_record_reads": {
        "layer": "core_process",
        "covers": [_c("DATA-01", "two real setup subprocesses retain DB/environment/installation IDs and read a created record")],
    },
    "tests/test_acceptance_matrix.py::test_data02_migration_failure_rolls_back_schema_and_existing_rows": {
        "covers": [_c("DATA-02", "injected v1 schema migration failure rolls back schema and existing request rows")],
    },
    "tests/test_acceptance_matrix.py::test_idem01_two_processes_create_one_record_and_replay_original": {
        "layer": "core_process",
        "covers": [_c("IDEM-01", "two real CLI processes use same request on first-use; one record/event and same response")],
    },
    "tests/test_acceptance_matrix.py::test_idem02_semantic_field_changes_conflict_without_mutating_original": {
        "layer": "core_process",
        "covers": [_c("IDEM-02", "operation/actor/session/target/revision/payload/context_refs field conflict matrix preserves original DB state")],
    },
    "tests/test_acceptance_matrix.py::test_claim02_other_session_and_old_token_cannot_release_or_finish": {
        "covers": [_c("CLAIM-02", "other-session release and previous-owner token finish both conflict")],
    },
    "tests/test_acceptance_matrix.py::test_claim03_confirmed_child_process_termination_allows_explicit_recovery": {
        "layer": "core_process",
        "covers": [_c("CLAIM-03", "child process exit is observed before explicit recovery and old token is rejected")],
    },
    "tests/test_acceptance_matrix.py::test_done01_missing_result_and_failed_verification_never_mark_done": {
        "covers": [_c("DONE-01", "missing result and failed verification cannot transition to Done")],
    },
    "tests/test_acceptance_matrix.py::test_done01_corrupt_evidence_and_unfinished_child_prevent_done": {
        "covers": [_c("DONE-01", "corrupt evidence and unfinished child prevent Done")],
    },
    "tests/test_acceptance_matrix.py::test_done01_unknown_verification_snapshot_cannot_finish": {
        "covers": [_c("DONE-01", "blocked verification with unknown workspace snapshot cannot finish")],
    },
    "tests/test_acceptance_matrix.py::test_done02_successful_finish_replay_keeps_one_completion_event": {
        "covers": [_c("DONE-02", "successful finish replays the same result and commits one completion event")],
    },
    "tests/test_acceptance_matrix.py::test_decision01_delegate_requires_scope_and_custom_can_be_replaced_by_select": {
        "covers": [_c("DECISION-01", "delegation scope is required; explicit custom decision can be superseded by selection and replayed")],
    },
    "tests/test_acceptance_matrix.py::test_log01_json_diagnostic_keeps_required_ids_and_excludes_secrets_paths": {
        "covers": [_c("LOG-01", "structured diagnostic preserves required IDs and redacts secret/transcript/path values")],
    },
    "tests/test_final_guards.py::test_obsolete_supersedes_cannot_create_multiple_current_decisions": {
        "covers": [_c("DECISION-01", "an already-Superseded decision cannot be reused to create two Current decisions")],
    },
    "tests/test_final_guards.py::test_failure_inserted_after_success_invalidates_even_when_clock_does_not_advance": {
        "expected_cases": 2,
        "covers": [_c("VERIFY-03", "row-inserted later fail invalidates prior pass when UTC timestamp is identical or moves backward")],
    },
    "tests/test_final_guards.py::test_explicit_and_configured_roots_do_not_require_path_home": {
        "expected_cases": 3,
        "covers": [_c("DATA-01", "explicit CLI roots and configured PMT/XDG roots initialize without Path.home; default root env values avoid eager home evaluation")],
    },
    "tests/test_cli_integration.py::test_cli_01_utf8_one_line_no_stdout_diagnostics": {
        "covers": [_c("CLI-01", "real CLI subprocess UTF-8 request, single JSON stdout line, request ID and exit code")],
    },
    "tests/test_cli_integration.py::test_cli_02_parse_errors_are_json_and_exit_two": {
        "expected_cases": 2,
        "covers": [_c("CLI-02", "malformed JSON and duplicate-key parse rejection")],
    },
    "tests/test_cli_integration.py::test_cli_02_schema_version_and_operation_rejected": {
        "expected_cases": 3,
        "covers": [_c("CLI-02", "missing envelope, unsupported protocol version, and unknown operation rejection")],
    },
    "tests/test_cli_integration.py::test_cli_02_oversized_json_rejected": {
        "covers": [_c("CLI-02", "1 MiB request limit")],
    },
    "tests/test_cli_integration.py::test_cli_02_invalid_payload_does_not_claim_or_mutate": {
        "covers": [_c("CLI-02", "invalid lifecycle payload is rejected without a successful claim")],
    },
    "tests/test_cli_integration.py::test_cli_03_cwd_with_spaces_and_korean_uses_configured_data_root": {
        "covers": [_c("CLI-03", "fresh external cwd with spaces and Korean path reads the selected data root")],
    },
    "tests/test_cli_integration.py::test_idem_01_same_request_key_replays_original_result": {
        "covers": [_c("IDEM-01", "same ID and reordered keys replay sequentially; concurrent first-use is not covered", False)],
    },
    "tests/test_cli_integration.py::test_idem_02_same_request_key_with_different_meaning_conflicts": {
        "covers": [_c("IDEM-02", "different payload conflicts; actor/session/operation/target field matrix is not covered", False)],
    },
    "tests/test_cli_integration.py::test_claim_01_two_independent_processes_claim_only_once": {
        "covers": [_c("CLAIM-01", "two real child CLI processes contend for one Item")],
    },
    "tests/test_cli_integration.py::test_rev_01_two_processes_with_same_revision_have_one_winner": {
        "covers": [_c("REV-01", "two real child CLI processes race with the same expected revision")],
    },
    "tests/test_end_to_end.py::test_done_02_full_workflow_and_new_session_context": {
        "covers": [_c("DONE-02", "subprocess evidence, resource, verification, explicit finish, and later-session read; same-request finish replay is missing", False)],
    },
    "tests/test_end_to_end.py::test_work_child_progress_is_derived_without_automatic_parent_completion": {
        "covers": [_c("READ-01", "real CLI read_context returns derived Work child progress without auto-completing the parent")],
    },
    "tests/test_hook_acceptance.py::test_hook_01_all_product_fixtures_use_real_cli_and_keep_only_minimal_event": {
        "layer": "hook_fixture", "expected_cases": 3,
        "covers": [_c("HOOK-01", "Codex, Claude, and OpenCode normal lifecycle fixtures reach the real CLI with minimal metadata")],
    },
    "tests/test_hook_acceptance.py::test_hook_03_unsupported_native_event_warns_without_storage": {
        "layer": "hook_fixture", "expected_cases": 3,
        "covers": [_c("HOOK-01", "unsupported native event fixture is rejected across products"),
                   _c("HOOK-03", "unsupported native events across products warn and do not create event/request rows")],
    },
    "tests/test_hook_acceptance.py::test_hook_03_pending_directory_write_failure_never_calls_core_or_claims_storage": {
        "layer": "hook_fixture",
        "covers": [_c("HOOK-03", "pending directory failure does not call PMT core or claim storage; simultaneous core/pending failure remains uncovered", False)],
    },
    "tests/test_hook_acceptance.py::test_hook_01_same_stable_native_occurrence_replays_one_committed_result": {
        "layer": "hook_fixture",
        "covers": [_c("HOOK-01", "same stable occurrence from all products is deduplicated by the real CLI")],
    },
    "tests/test_hook_acceptance.py::test_hook_01_open_code_without_occurrence_id_only_deduplicates_pending_replay": {
        "layer": "hook_fixture",
        "covers": [_c("HOOK-01", "separate OpenCode events without occurrence IDs remain distinct")],
    },
    "tests/test_hook_acceptance.py::test_stop_01_reverse_stop_session_end_and_idle_never_change_item_state": {
        "layer": "hook_fixture",
        "covers": [_c("STOP-01", "reverse-order Codex/Claude/OpenCode Stop, session-end, idle, and subagent-stop never changes item state"),
                   _c("HOOK-01", "reverse-order lifecycle fixture is persisted as observations without finishing work")],
    },
    "tests/test_hook_acceptance.py::test_hook_02_real_database_busy_keeps_pending_and_replay_commits_once": {
        "layer": "hook_fixture",
        "covers": [_c("HOOK-02", "actual SQLite busy preserves pending envelope then one replay commits")],
    },
    "tests/test_hook_acceptance.py::test_hook_02_actual_cli_timeout_preserves_original_pending_ids_for_replay": {
        "layer": "hook_fixture",
        "covers": [_c("HOOK-02", "actual Python CLI timeout keeps original pending IDs and later replay commits once")],
    },
    "tests/test_hook_acceptance.py::test_hook_02_actual_database_io_failure_keeps_pending_until_repair": {
        "layer": "hook_fixture",
        "covers": [_c("HOOK-02", "actual database file I/O failure retains pending input until repair and one replay")],
    },
    "tests/test_hook_acceptance.py::test_hook_03_database_and_pending_root_failure_is_visible_without_false_save": {
        "layer": "hook_fixture",
        "covers": [_c("HOOK-03", "simultaneous database-root and pending-root write failures are visible and never claim storage")],
    },
    "tests/test_fixture_contract.py::test_fixture_request_has_protocol_v1_and_stable_session": {"covers": []},
    "tests/test_fixture_contract.py::test_fixture_roots_are_separate_and_user_scoped": {"covers": []},
    "tests/test_foundation.py::test_schema_initializes_and_ids_persist": {
        "covers": [_c("DATA-01", "schema and IDs persist after a second Database constructor; setup in a separate process is not covered", False)],
    },
    "tests/test_foundation.py::test_run_request_replay_conflict_and_atomic_error": {
        "covers": [_c("IDEM-01", "in-process successful request replay; does not cover concurrent first-use", False),
                   _c("IDEM-02", "in-process payload conflict only; field matrix is not covered", False)],
    },
    "tests/test_hooks.py::test_codex_stable_occurrence_ids_and_no_prompt_copy": {
        "covers": [_c("HOOK-01", "Codex fixture IDs and prompt exclusion; no cross-product event matrix", False),
                   _c("LOG-01", "hook normalization excludes prompt content; diagnostic-log redaction is not exercised", False)],
    },
    "tests/test_hooks.py::test_native_occurrence_namespace_separates_product_and_session": {"covers": []},
    "tests/test_hooks.py::test_missing_occurrence_gets_new_uuid_with_replay_only_scope": {
        "covers": [_c("HOOK-01", "missing native occurrence ID documents replay-only dedup scope", False)],
    },
    "tests/test_hooks.py::test_opencode_keeps_only_allowlisted_metadata": {
        "covers": [_c("HOOK-01", "OpenCode fixture allowlist only; Codex/Claude and reverse-order cases are not covered", False)],
    },
    "tests/test_hooks.py::test_profile_fallback_is_persistent_separate_and_rejects_corruption": {"covers": []},
    "tests/test_hooks.py::test_pending_saved_before_cli_and_removed_only_after_ok": {"covers": []},
    "tests/test_hooks.py::test_pending_retained_on_cli_failure": {
        "covers": [_c("HOOK-02", "Claude fixture keeps pending after mocked CLI failure; real busy/I/O retry is not exercised", False)],
    },
    "tests/test_hooks.py::test_timeout_leaves_same_pending_envelope_for_replay": {
        "covers": [_c("HOOK-02", "mocked timeout retains stable pending IDs and replays once", False)],
    },
    "tests/test_hooks.py::test_explicit_pending_replay_uses_original_ids": {
        "covers": [_c("HOOK-02", "pending replay preserves IDs; dual PMT-and-pending write failure is not covered", False)],
    },
    "tests/test_hooks.py::test_unsupported_native_event_rejected": {"covers": []},
    "tests/test_hooks.py::test_sensitive_values_are_excluded_without_banning_prompt_event_type": {
        "covers": [_c("LOG-01", "native normalized event excludes test secrets/transcript; logger output itself is not tested", False)],
    },
    "tests/test_hooks.py::test_opencode_tool_completion_uses_documented_hook_and_packaged_bridge": {"covers": []},
    "tests/test_hooks.py::test_codex_windows_commands_quote_resolved_plugin_root": {"covers": []},
    "tests/test_hooks.py::test_claude_command_args_use_official_exec_form": {"covers": []},
    "tests/test_lifecycle.py::test_scope_record_event_and_idempotent_replay": {
        "covers": [_c("EVENT-01", "different request IDs with the same event ID store one occurrence"),
                   _c("IDEM-01", "in-process same-request replay; concurrent first-use remains untested", False),
                   _c("STOP-01", "Stop event leaves Item Planned; not the requested per-product Stop/idle fixtures", False)],
    },
    "tests/test_lifecycle.py::test_scope_relationship_remote_normalization_and_no_merge": {
        "covers": [_c("DATA-03", "scope parent rules, URL credential normalization, and fork non-merge")],
    },
    "tests/test_lifecycle.py::test_claim_release_recover_and_stale_owner": {
        "covers": [_c("CLAIM-02", "wrong session and old token cannot release; stale-token finish case is not covered", False),
                   _c("CLAIM-03", "manual recovery rotates token and refuses previous token")],
    },
    "tests/test_lifecycle.py::test_competing_claims_commit_once": {"covers": []},
    "tests/test_lifecycle.py::test_change_revision_decision_and_finish_fails_closed": {
        "covers": [_c("DONE-01", "missing/invalid verification ID fails closed; other failed criteria and child cases are not covered", False),
                   _c("DECISION-01", "explicit user selection; delegation and supersession matrix is not covered", False)],
    },
    "tests/test_lifecycle.py::test_profile_concurrent_creation_and_corruption_preservation": {
        "covers": [_c("DATA-01", "concurrent profile initialization only; second-process DB identity read is not covered", False)],
    },
    "tests/test_lifecycle.py::test_v1_request_owner_migration_and_diagnostic_warning": {"covers": []},
    "tests/test_lifecycle.py::test_alias_conflict_and_unsupported_schema_preservation": {"covers": []},
    "tests/test_packaging.py::test_pkg_01_three_separate_bundles_manifest_and_zip_hashes": {
        "covers": [_c("PKG-01", "three generated product folders/ZIPs, file hashes, manifests, and excluded checkout paths")],
    },
    "tests/test_packaging.py::test_pkg_01_standalone_cli_works_from_unrelated_cwd": {
        "covers": [_c("PKG-01", "built folder CLI succeeds from an unrelated UTF-8 cwd")],
    },
    "tests/test_packaging.py::test_pkg_01_second_version_does_not_replace_earlier_bundle": {
        "covers": [_c("PKG-01", "0.1.0 then 0.1.1 build preserves earlier folder and archive")],
    },
    "tests/test_packaging.py::test_pkg_01_existing_version_is_preserved_and_refused": {
        "covers": [_c("PKG-01", "same-version output is refused without changing earlier package")],
    },
    "tests/test_packaging.py::test_pkg_01_source_mutation_during_copy_aborts_publication": {
        "covers": [_c("PKG-01", "source mutation during copy prevents publication")],
    },
    "tests/test_packaging.py::test_pkg_01_retries_one_temporary_windows_publish_denial": {
        "covers": [_c("PKG-01", "single transient Windows access denial retries and publishes the package")],
    },
    "tests/test_packaging.py::test_pkg_01_persistent_windows_publish_denial_is_bounded_and_structured": {
        "covers": [_c("PKG-01", "persistent Windows sharing violation stops within the bounded retry window, returns OS details, and removes stage")],
    },
    "tests/test_packaging.py::test_pkg_01_source_drift_during_publish_retry_aborts_and_cleans_stage": {
        "covers": [_c("PKG-01", "source changes during the publish retry wait are caught before retry and stage is cleaned")],
    },
    "tests/test_queries.py::test_read_01_scoped_records_decisions_next_watch_and_cursor": {
        "covers": [_c("READ-01", "scope filtering, query, decisions, next/watch, and stable cursor")],
    },
    "tests/test_queries.py::test_read_01_record_selection_includes_only_same_scope_descendants": {
        "covers": [_c("READ-01", "selected-record descendants remain within the record scope")],
    },
    "tests/test_queries.py::test_read_01_scope_includes_descendant_projects_but_not_siblings": {
        "covers": [_c("READ-01", "scope hierarchy includes descendants and excludes siblings")],
    },
    "tests/test_queries.py::test_read_02_summary_is_bounded_and_keeps_current_warning": {
        "covers": [_c("READ-02", "4,500-character structural summary retains the current warning")],
    },
    "tests/test_resources.py::test_resource_stages_hashes_and_registers_reference": {
        "covers": [_c("RESOURCE-01", "staging, hash, ready artifact, and owner reference")],
    },
    "tests/test_resources.py::test_register_replay_and_concurrent_same_request_publish_once": {
        "covers": [_c("RESOURCE-01", "concurrent same-request registration publishes a single immutable artifact")],
    },
    "tests/test_resources.py::test_resource_rejects_escape_and_symlink": {
        "covers": [_c("RESOURCE-03", "external path and symlink source are rejected; privilege-sensitive helper case", False)],
    },
    "tests/test_resources.py::test_diagnose_reports_missing_corrupt_orphan_and_retention_candidate": {
        "covers": [_c("RESOURCE-02", "diagnose finds missing/corrupt references, orphan file, and retention candidate")],
    },
    "tests/test_resources.py::test_register_same_request_changed_payload_conflicts_before_second_copy": {"covers": []},
    "tests/test_resources.py::test_register_rejects_foreign_scope_and_owner_before_file_job": {"covers": []},
    "tests/test_resources.py::test_register_db_failure_leaves_diagnosable_orphan_and_failed_job": {
        "covers": [_c("RESOURCE-02", "DB failure after file publication is found as an orphan")],
    },
    "tests/test_resources.py::test_diagnose_excludes_runtime_data_locations": {"covers": []},
    "tests/test_resources.py::test_diagnose_reports_resource_links_without_traversing_them": {
        "covers": [_c("RESOURCE-03", "diagnosis reports resource link paths without traversing; privilege-sensitive helper case", False)],
    },
    "tests/test_resources.py::test_windows_junctions_are_rejected_without_traversing_target": {
        "covers": [_c("RESOURCE-03", "Windows junction is rejected without traversing its target")],
    },
    "tests/test_resources.py::test_active_maintenance_blocks_constructor_side_effects_and_resource_jobs": {"covers": []},
    "tests/test_backup.py::test_backup_restore_roundtrip_preserves_database_and_blobs": {
        "covers": [_c("BACKUP-01", "backup/restore roundtrip preserves DB and artifact hash")],
    },
    "tests/test_backup.py::test_restore_rejects_corrupt_manifest_blob_without_publishing": {
        "covers": [_c("BACKUP-02", "corrupt manifest/blob cannot publish restore")],
    },
    "tests/test_backup.py::test_backup_rejects_active_resource_job_and_nonempty_destination": {
        "covers": [_c("BACKUP-01", "active file job and nonempty destination block backup")],
    },
    "tests/test_backup.py::test_backup_same_request_concurrent_and_replay_copies_once": {
        "covers": [_c("BACKUP-01", "concurrent backup replay and same-request idempotency")],
    },
    "tests/test_backup.py::test_backup_maintenance_blocks_independent_cli_writer_and_resource_registration": {
        "layer": "core_process",
        "covers": [_c("BACKUP-01", "independent CLI writer and resource registration are blocked during maintenance; reader remains valid")],
    },
    "tests/test_backup.py::test_backup_copy_failure_marks_incomplete_and_releases_maintenance": {
        "covers": [_c("BACKUP-02", "failed backup is marked incomplete and maintenance is released")],
    },
    "tests/test_backup.py::test_restore_bad_schema_and_missing_blob_leave_live_records_unchanged": {
        "covers": [_c("BACKUP-02", "bad schema and missing blob preserve live records")],
    },
    "tests/test_backup.py::test_restore_same_request_concurrent_publishes_once": {"covers": []},
    "tests/test_verification.py::test_verify_01_matching_snapshot_and_real_ready_artifact_is_reusable": {
        "covers": [_c("VERIFY-01", "matching current workspace/runtime snapshot plus real ready evidence is reusable")],
    },
    "tests/test_verification.py::test_verify_01_reuse_is_limited_by_scope_workspace_criteria_command_and_environment": {
        "covers": [_c("VERIFY-01", "matching candidate across target scope/workspace/criteria/command/environment conditions")],
    },
    "tests/test_verification.py::test_verify_01_subset_passes_union_to_satisfy_all_finish_criteria": {
        "covers": [_c("VERIFY-01", "criterion-specific evidence unions only when they cover every current criterion")],
    },
    "tests/test_verification.py::test_verify_01_id_only_criterion_object_matches_its_canonical_string_form": {
        "covers": [_c("VERIFY-01", "criterion ID hash is stable between canonical string and ID-only object")],
    },
    "tests/test_verification.py::test_verify_01_missing_evidence_never_accepts_pass": {"covers": []},
    "tests/test_verification.py::test_verify_02_actual_dirty_and_untracked_workspace_changes_cannot_reuse": {
        "covers": [_c("VERIFY-02", "actual untracked workspace content changes the fingerprint")],
    },
    "tests/test_verification.py::test_verify_02_content_command_and_criteria_changes_stale_success": {
        "covers": [_c("VERIFY-02", "command, workspace content, and criterion changes stale success")],
    },
    "tests/test_verification.py::test_verify_02_missing_workspace_makes_pass_impossible": {
        "covers": [_c("VERIFY-02", "unknown current workspace blocks a pass")],
    },
    "tests/test_verification.py::test_verify_02_before_snapshot_is_required_and_execution_change_refuses_pass": {
        "covers": [_c("VERIFY-02", "before-run fingerprint is required and a workspace change before record blocks pass")],
    },
    "tests/test_verification.py::test_verify_02_configuration_dependency_inputs_runtime_change_invalidates_pass": {
        "expected_cases": 4,
        "covers": [_c("VERIFY-02", "separate configuration, dependency, input-fixture, and runtime changes stale reuse")],
    },
    "tests/test_verification.py::test_verify_03_later_failure_or_corrupt_evidence_invalidates_prior_pass": {
        "covers": [_c("VERIFY-03", "later non-pass verification invalidates the older success")],
    },
    "tests/test_verification.py::test_verify_03_failure_in_an_independent_scope_does_not_stale_candidate": {
        "covers": [_c("VERIFY-03", "failure in an independent scope does not stale the current candidate")],
    },
    "tests/test_verification.py::test_verify_03_evidence_hash_change_invalidates_prior_pass": {
        "covers": [_c("VERIFY-03", "corrupted ready evidence invalidates reuse and finish validation")],
    },
    "tests/test_lifecycle_extensions.py::test_log02_real_stream_failure_keeps_commit_warns_and_replays": {
        "covers": [_c("LOG-02", "real failing logging stream preserves the committed write, warning, and replay")],
    },
    "tests/test_lifecycle_extensions.py::test_record_event_preserves_allowlisted_source_metadata_and_drops_prompt_secrets": {
        "covers": [_c("LOG-01", "event source metadata allowlist drops prompt/token values; diagnostic log is not tested", False)],
    },
}

# Phase IDs pass only when every required exact node and the specified coverage gaps
# are satisfied. A passing partial node can never override a missing scenario.
ID_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "DATA-01": {"nodes": ["tests/test_acceptance_matrix.py::test_data01_cli_setup_is_stable_across_processes_and_record_reads",
                            "tests/test_final_guards.py::test_explicit_and_configured_roots_do_not_require_path_home"], "not_covered": []},
    "DATA-02": {"nodes": ["tests/test_acceptance_matrix.py::test_data02_migration_failure_rolls_back_schema_and_existing_rows"], "not_covered": []},
    "DATA-03": {"nodes": ["tests/test_lifecycle.py::test_scope_relationship_remote_normalization_and_no_merge"], "not_covered": []},
    "TX-01": {"nodes": ["tests/test_acceptance.py::test_tx_01_event_ledger_failure_rolls_back_record"], "not_covered": []},
    "IDEM-01": {"nodes": ["tests/test_acceptance_matrix.py::test_idem01_two_processes_create_one_record_and_replay_original"], "not_covered": []},
    "IDEM-02": {"nodes": ["tests/test_acceptance_matrix.py::test_idem02_semantic_field_changes_conflict_without_mutating_original"], "not_covered": []},
    "EVENT-01": {"nodes": ["tests/test_lifecycle.py::test_scope_record_event_and_idempotent_replay"], "not_covered": []},
    "EVENT-02": {"nodes": ["tests/test_acceptance.py::test_event_02_repeated_prompt_ids_and_reverse_stop_never_finish"], "not_covered": []},
    "REV-01": {"nodes": ["tests/test_cli_integration.py::test_rev_01_two_processes_with_same_revision_have_one_winner"], "not_covered": []},
    "CLAIM-01": {"nodes": ["tests/test_cli_integration.py::test_claim_01_two_independent_processes_claim_only_once"], "not_covered": []},
    "CLAIM-02": {"nodes": ["tests/test_acceptance_matrix.py::test_claim02_other_session_and_old_token_cannot_release_or_finish"], "not_covered": []},
    "CLAIM-03": {"nodes": ["tests/test_acceptance_matrix.py::test_claim03_confirmed_child_process_termination_allows_explicit_recovery"], "not_covered": []},
    "DONE-01": {"nodes": ["tests/test_acceptance_matrix.py::test_done01_missing_result_and_failed_verification_never_mark_done",
                             "tests/test_acceptance_matrix.py::test_done01_corrupt_evidence_and_unfinished_child_prevent_done",
                             "tests/test_acceptance_matrix.py::test_done01_unknown_verification_snapshot_cannot_finish"], "not_covered": []},
    "DONE-02": {"nodes": ["tests/test_acceptance_matrix.py::test_done02_successful_finish_replay_keeps_one_completion_event"], "not_covered": []},
    "DECISION-01": {"nodes": ["tests/test_acceptance_matrix.py::test_decision01_delegate_requires_scope_and_custom_can_be_replaced_by_select",
                                "tests/test_acceptance.py::test_decision_01_explicit_choice_supersession_and_stale_rejection",
                                "tests/test_final_guards.py::test_obsolete_supersedes_cannot_create_multiple_current_decisions"], "not_covered": []},
    "READ-01": {"nodes": ["tests/test_queries.py::test_read_01_scoped_records_decisions_next_watch_and_cursor",
                            "tests/test_queries.py::test_read_01_record_selection_includes_only_same_scope_descendants",
                            "tests/test_queries.py::test_read_01_scope_includes_descendant_projects_but_not_siblings",
                            "tests/test_end_to_end.py::test_work_child_progress_is_derived_without_automatic_parent_completion"], "not_covered": []},
    "READ-02": {"nodes": ["tests/test_queries.py::test_read_02_summary_is_bounded_and_keeps_current_warning"], "not_covered": []},
    "VERIFY-01": {"nodes": ["tests/test_verification.py::test_verify_01_matching_snapshot_and_real_ready_artifact_is_reusable",
                              "tests/test_verification.py::test_verify_01_reuse_is_limited_by_scope_workspace_criteria_command_and_environment",
                              "tests/test_verification.py::test_verify_01_subset_passes_union_to_satisfy_all_finish_criteria",
                              "tests/test_verification.py::test_verify_01_id_only_criterion_object_matches_its_canonical_string_form"], "not_covered": []},
    "VERIFY-02": {"nodes": ["tests/test_verification.py::test_verify_02_actual_dirty_and_untracked_workspace_changes_cannot_reuse",
                              "tests/test_verification.py::test_verify_02_missing_workspace_makes_pass_impossible",
                              "tests/test_verification.py::test_verify_02_content_command_and_criteria_changes_stale_success",
                              "tests/test_verification.py::test_verify_02_before_snapshot_is_required_and_execution_change_refuses_pass",
                              "tests/test_verification.py::test_verify_02_configuration_dependency_inputs_runtime_change_invalidates_pass"], "not_covered": []},
    "VERIFY-03": {"nodes": ["tests/test_verification.py::test_verify_03_later_failure_or_corrupt_evidence_invalidates_prior_pass",
                              "tests/test_verification.py::test_verify_03_failure_in_an_independent_scope_does_not_stale_candidate",
                              "tests/test_verification.py::test_verify_03_evidence_hash_change_invalidates_prior_pass",
                              "tests/test_final_guards.py::test_failure_inserted_after_success_invalidates_even_when_clock_does_not_advance"], "not_covered": []},
    "RESOURCE-01": {"nodes": ["tests/test_resources.py::test_resource_stages_hashes_and_registers_reference",
                                "tests/test_resources.py::test_register_replay_and_concurrent_same_request_publish_once"], "not_covered": []},
    "RESOURCE-02": {"nodes": ["tests/test_resources.py::test_diagnose_reports_missing_corrupt_orphan_and_retention_candidate",
                                "tests/test_resources.py::test_register_db_failure_leaves_diagnosable_orphan_and_failed_job"], "not_covered": []},
    "RESOURCE-03": {"nodes": ["tests/test_resources.py::test_windows_junctions_are_rejected_without_traversing_target"], "not_covered": [],
                     "notes": ["On Windows, a privilege-blocked file-symlink helper is reported individually as blocked; the required junction-boundary test is separate."]},
    "BACKUP-01": {"nodes": ["tests/test_backup.py::test_backup_restore_roundtrip_preserves_database_and_blobs",
                              "tests/test_backup.py::test_backup_rejects_active_resource_job_and_nonempty_destination",
                              "tests/test_backup.py::test_backup_same_request_concurrent_and_replay_copies_once",
                              "tests/test_backup.py::test_backup_maintenance_blocks_independent_cli_writer_and_resource_registration"],
                  "not_covered": [], "notes": ["GC concurrency is intentionally excluded; phase 1 reports retention candidates but does not run automatic GC."]},
    "BACKUP-02": {"nodes": ["tests/test_backup.py::test_restore_rejects_corrupt_manifest_blob_without_publishing",
                              "tests/test_backup.py::test_restore_bad_schema_and_missing_blob_leave_live_records_unchanged",
                              "tests/test_backup.py::test_backup_copy_failure_marks_incomplete_and_releases_maintenance"], "not_covered": []},
    "CLI-01": {"nodes": ["tests/test_cli_integration.py::test_cli_01_utf8_one_line_no_stdout_diagnostics"], "not_covered": []},
    "CLI-02": {"nodes": ["tests/test_cli_integration.py::test_cli_02_parse_errors_are_json_and_exit_two",
                           "tests/test_cli_integration.py::test_cli_02_schema_version_and_operation_rejected",
                           "tests/test_cli_integration.py::test_cli_02_oversized_json_rejected",
                           "tests/test_cli_integration.py::test_cli_02_invalid_payload_does_not_claim_or_mutate"], "not_covered": []},
    "CLI-03": {"nodes": ["tests/test_cli_integration.py::test_cli_03_cwd_with_spaces_and_korean_uses_configured_data_root"], "not_covered": []},
    "RECOVER-01": {"nodes": ["tests/test_acceptance.py::test_recover_01_child_exit_boundary_and_request_replay"], "not_covered": []},
    "STOP-01": {"nodes": ["tests/test_hook_acceptance.py::test_stop_01_reverse_stop_session_end_and_idle_never_change_item_state"], "not_covered": []},
    "HOOK-01": {"nodes": ["tests/test_hook_acceptance.py::test_hook_01_all_product_fixtures_use_real_cli_and_keep_only_minimal_event",
                             "tests/test_hook_acceptance.py::test_hook_01_same_stable_native_occurrence_replays_one_committed_result",
                             "tests/test_hook_acceptance.py::test_hook_01_open_code_without_occurrence_id_only_deduplicates_pending_replay",
                             "tests/test_hook_acceptance.py::test_hook_03_unsupported_native_event_warns_without_storage",
                             "tests/test_hook_acceptance.py::test_stop_01_reverse_stop_session_end_and_idle_never_change_item_state"], "not_covered": []},
    "HOOK-02": {"nodes": ["tests/test_hook_acceptance.py::test_hook_02_real_database_busy_keeps_pending_and_replay_commits_once",
                             "tests/test_hook_acceptance.py::test_hook_02_actual_cli_timeout_preserves_original_pending_ids_for_replay",
                             "tests/test_hook_acceptance.py::test_hook_02_actual_database_io_failure_keeps_pending_until_repair"], "not_covered": []},
    "HOOK-03": {"nodes": ["tests/test_hook_acceptance.py::test_hook_03_unsupported_native_event_warns_without_storage",
                             "tests/test_hook_acceptance.py::test_hook_03_database_and_pending_root_failure_is_visible_without_false_save"], "not_covered": []},
    "LOG-01": {"nodes": ["tests/test_acceptance_matrix.py::test_log01_json_diagnostic_keeps_required_ids_and_excludes_secrets_paths"], "not_covered": []},
    "LOG-02": {"nodes": ["tests/test_acceptance.py::test_log_02_diagnostic_failure_keeps_committed_response_and_warning",
                            "tests/test_lifecycle_extensions.py::test_log02_real_stream_failure_keeps_commit_warns_and_replays"], "not_covered": []},
    "LOG-03": {"nodes": ["tests/test_acceptance.py::test_tx_01_event_ledger_failure_rolls_back_record",
                            "tests/test_acceptance.py::test_log_03_request_ledger_failure_rolls_back_record_and_event"], "not_covered": []},
    "PKG-01": {"nodes": ["tests/test_packaging.py::test_pkg_01_three_separate_bundles_manifest_and_zip_hashes",
                           "tests/test_packaging.py::test_pkg_01_standalone_cli_works_from_unrelated_cwd",
                           "tests/test_packaging.py::test_pkg_01_second_version_does_not_replace_earlier_bundle",
                           "tests/test_packaging.py::test_pkg_01_existing_version_is_preserved_and_refused",
                           "tests/test_packaging.py::test_pkg_01_source_mutation_during_copy_aborts_publication",
                           "tests/test_packaging.py::test_pkg_01_retries_one_temporary_windows_publish_denial",
                           "tests/test_packaging.py::test_pkg_01_persistent_windows_publish_denial_is_bounded_and_structured",
                           "tests/test_packaging.py::test_pkg_01_source_drift_during_publish_retry_aborts_and_cleans_stage"], "not_covered": []},
}

SECRET = re.compile(r"(?i)(bearer\s+)[^\s,;]+|((?:token|secret|password|api[_-]?key)\s*[=:]\s*)[^\s,;]+")
WINDOWS_ABSOLUTE = re.compile(r"(?i)(?<![\w])(?:[a-z]:\\|\\\\)[^\s\"']+")
POSIX_TEMP = re.compile(r"(?<![\w])/(?:tmp|var/tmp|private/tmp)/[^\s\"']+")
OPTIONAL_SYMLINK_SKIP_NODES = {
    "tests/test_resources.py::test_resource_rejects_escape_and_symlink",
    "tests/test_resources.py::test_diagnose_reports_resource_links_without_traversing_them",
}
GIT_EXCLUDE_SEGMENTS = {
    ".pmt-test", ".git", ".venv", "venv", "node_modules", ".pytest_cache", ".pytest-tmp",
    "__pycache__", "logs", "log", "auth", "credentials", "secrets", "hook-pending",
    ".aws", ".ssh", ".azure", ".gcloud",
}
GIT_EXCLUDE_NAMES = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc", ".netrc",
                     "credentials.json", "token.json", "secrets.json", "id_rsa", "id_ed25519"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scrub(text: str, limit: int = 12000) -> str:
    text = SECRET.sub(lambda m: (m.group(1) or "") + "[REDACTED]" if m.group(1) else m.group(2) + "[REDACTED]", text)
    text = WINDOWS_ABSOLUTE.sub("<local-path>", text)
    text = POSIX_TEMP.sub("<temporary-path>", text)
    return text[:limit]


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                          capture_output=True, check=False)


def _excluded_git_path(relative: str) -> bool:
    path = PurePosixPath(relative.replace("\\", "/"))
    if any(part.casefold() in GIT_EXCLUDE_SEGMENTS for part in path.parts):
        return True
    name = path.name.casefold()
    return name in GIT_EXCLUDE_NAMES or name.startswith("auth.") or name.endswith((".pem", ".key", ".p12", ".pfx"))


def _git_status_paths(raw: str) -> list[str]:
    """Parse `git status --porcelain=v1 -z`; no path is opened here."""
    pieces = raw.split("\0")
    result = []
    index = 0
    while index < len(pieces):
        entry = pieces[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        result.append(path)
        # For rename/copy records porcelain v1 -z emits the original name next.
        if "R" in status or "C" in status:
            index += 1
    return result


def _find_git_root(path: Path) -> Path | None:
    cursor = path.resolve()
    for candidate in (cursor, *cursor.parents):
        marker = candidate / ".git"
        if marker.is_dir() or marker.is_file():
            return candidate
    return None


def git_fingerprint(root: Path) -> dict[str, Any]:
    git_root = _find_git_root(root)
    if git_root is None:
        return {"commit": None, "dirty_fingerprint": None, "dirty_paths": [],
                "git_error": "unavailable", "safe_directory": "<repo-root>"}
    safe = git_root.as_posix()
    prefix = ["git", "-c", f"safe.directory={safe}", "-C", safe]
    head = run(prefix + ["rev-parse", "HEAD"], git_root)
    status = run(prefix + ["status", "--porcelain=v1", "--untracked-files=all", "-z"], git_root)
    if head.returncode or status.returncode:
        return {"commit": None, "dirty_fingerprint": None, "dirty_paths": [],
                "git_error": "unavailable", "safe_directory": "<project-root>"}
    included, excluded_count = [], 0
    for relative in _git_status_paths(status.stdout):
        normalized = relative.replace("\\", "/")
        if not normalized or Path(normalized).is_absolute() or _excluded_git_path(normalized):
            excluded_count += 1
            continue
        candidate = Path(os.path.abspath(git_root / Path(relative)))
        try:
            label = candidate.relative_to(git_root).as_posix()
        except ValueError:
            excluded_count += 1
            continue
        if _excluded_git_path(label):
            excluded_count += 1
            continue
        included.append(label)
    digest = hashlib.sha256()
    hashes = []
    for relative in sorted(set(included)):
        target = git_root / Path(relative)
        parent = target.parent
        has_link_parent = False
        while parent != git_root and git_root in parent.parents:
            if parent.is_symlink():
                has_link_parent = True
                break
            parent = parent.parent
        if target.is_symlink() or has_link_parent:
            marker = "symlink-not-followed"
        else:
            try:
                target.resolve().relative_to(git_root)
                if target.is_file():
                    marker = _sha256_file(target)
                elif target.is_dir():
                    marker = "directory-or-submodule-not-hashed"
                else:
                    marker = "missing"
            except (OSError, ValueError):
                marker = "unreadable-or-outside-root"
        hashes.append({"path": relative, "sha256": marker})
        digest.update(relative.encode("utf-8", "replace"))
        digest.update(marker.encode("ascii"))
    digest.update(str(excluded_count).encode("ascii"))
    return {"commit": head.stdout.strip(), "dirty_fingerprint": digest.hexdigest(),
            "dirty_paths": hashes, "excluded_path_count": excluded_count,
            "safe_directory": "<repo-root>"}


def _case_node(case: ET.Element) -> tuple[str, str, str]:
    classname = case.attrib.get("classname", "")
    module_name = classname.rsplit(".", 1)[-1]
    module_path = f"tests/{module_name}.py"
    test_name = case.attrib.get("name", "")
    base_name = test_name.split("[", 1)[0]
    return f"{module_path}::{base_name}", f"{module_path}::{test_name}", module_path


def _testcase_result(case: ET.Element) -> tuple[str, str | None]:
    skipped = case.find("skipped")
    if skipped is not None:
        reason = scrub(skipped.attrib.get("message", "") + "\n" + (skipped.text or ""), 1000)
        return "blocked", reason or "pytest skipped this test; skip is never pass"
    failure = case.find("failure")
    if failure is None:
        failure = case.find("error")
    if failure is not None:
        reason = scrub(failure.attrib.get("message", "") + "\n" + (failure.text or ""), 3000)
        if "No module named 'pmt'" in reason or 'No module named "pmt"' in reason:
            return "blocked", reason
        return "fail", reason
    return "pass", None


def _sanitize_junit_file(path: Path) -> str | None:
    """Rewrite raw JUnit without hostname, raw absolute paths, or unsanitized traces."""
    if not path.exists():
        return None
    root = ET.parse(path).getroot()
    for element in root.iter():
        element.attrib.pop("hostname", None)
        element.attrib.pop("timestamp", None)
        for key, value in list(element.attrib.items()):
            element.attrib[key] = scrub(value, 2000)
        if element.text:
            element.text = scrub(element.text, 6000)
        if element.tail:
            element.tail = scrub(element.tail, 2000)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _xml_suite_errors(root: ET.Element) -> list[str]:
    errors = []
    for node in root.findall(".//error"):
        if node.tag == "error":
            errors.append(scrub(node.attrib.get("message", "") + "\n" + (node.text or ""), 2000))
    return errors


def _case_record(case: ET.Element) -> dict[str, Any]:
    mapping_node, node_id, module_path = _case_node(case)
    result, failure = _testcase_result(case)
    mapping = TEST_NODE_MAP.get(mapping_node)
    layer = (mapping or {}).get("layer") or MODULE_LAYERS.get(module_path, "unclassified")
    covers = [] if mapping is None else mapping.get("covers", [])
    optional_symlink_skip = (result == "blocked" and mapping_node in OPTIONAL_SYMLINK_SKIP_NODES and
                             failure is not None and "cannot create symlinks" in failure.casefold())
    return {"node_id": node_id, "mapping_node": mapping_node, "layer": layer,
            "result": result, "duration_seconds": float(case.attrib.get("time", "0")),
            "coverage": covers, "failure_evidence": failure,
            "optional_platform_skip": optional_symlink_skip}


def parse_junit(path: Path) -> tuple[list[dict[str, Any]], list[str], int]:
    if not path.exists():
        return [], [], 0
    root = ET.parse(path).getroot()
    cases = [_case_record(case) for case in root.findall(".//testcase")]
    skipped_count = sum(case["result"] == "blocked" for case in cases)
    return cases, _xml_suite_errors(root), skipped_count


def _node_result(cases: list[dict[str, Any]], expected_cases: int = 1) -> str:
    if not cases:
        return "not_run"
    if len(cases) < expected_cases:
        return "not_run"
    results = {case["result"] for case in cases}
    if "fail" in results:
        return "fail"
    if "blocked" in results:
        return "blocked"
    if "stale" in results:
        return "stale"
    if results == {"pass"}:
        return "pass"
    return "not_run"


def summarize_ids(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for case in cases:
        for cover in case.get("coverage", []):
            grouped.setdefault(cover["test_id"], {}).setdefault(case["mapping_node"], []).append(case)
    summary = {}
    for test_id in G1_IDS + G2_IDS:
        observations = grouped.get(test_id, {})
        specification = ID_REQUIREMENTS.get(test_id, {"nodes": [], "not_covered": ["no coverage specification"]})
        required_nodes = specification["nodes"]
        node_results = {node: _node_result(observations.get(node, []), TEST_NODE_MAP.get(node, {}).get("expected_cases", 1))
                        for node in required_nodes}
        mapped_nodes = [key for key, entry in TEST_NODE_MAP.items()
                        if any(c["test_id"] == test_id for c in entry.get("covers", []))]
        partial_nodes = [key for key in mapped_nodes
                         if key not in required_nodes or any(c["test_id"] == test_id and not c["complete"]
                                                             for c in TEST_NODE_MAP[key].get("covers", []))]
        partial_results = {node: _node_result(observations.get(node, []), TEST_NODE_MAP[node].get("expected_cases", 1))
                           for node in partial_nodes}
        blocking_partial_nodes = [node for node in partial_nodes if not (
            observations.get(node) and all(case.get("optional_platform_skip", False)
                                            for case in observations[node]))]
        effective_partial_states = [partial_results[node] for node in blocking_partial_nodes]
        if any(state == "fail" for state in (*node_results.values(), *effective_partial_states)):
            result, reasons = "fail", ["a required mapped test node failed"]
        elif any(state == "blocked" for state in (*node_results.values(), *effective_partial_states)):
            result, reasons = "blocked", ["a required mapped test node was skipped or blocked"]
        elif any(state == "stale" for state in (*node_results.values(), *effective_partial_states)):
            result, reasons = "stale", ["target changed while the mapped tests were running"]
        elif specification["not_covered"]:
            result, reasons = "not_run", list(specification["not_covered"])
        elif not required_nodes:
            result, reasons = "not_run", ["no complete checked-in node mapping for all required scenarios"]
        elif any(state == "not_run" for state in node_results.values()):
            result, reasons = "not_run", ["one or more required mapped test nodes were absent from JUnit"]
        else:
            result, reasons = "pass", []
        summary[test_id] = {"test_id": test_id, "result": result, "required_nodes": node_results,
                            "partial_nodes": [{"node_id": node, "observed_result": partial_results[node],
                                               "coverage": [c["coverage"] for c in TEST_NODE_MAP[node].get("covers", []) if c["test_id"] == test_id]}
                                              for node in partial_nodes],
                            "reasons": reasons, "notes": specification.get("notes", [])}
    return summary


def _stage_result(ids: list[str], results: dict[str, dict[str, Any]], suite_result: str | None = None) -> str:
    states = [results[test_id]["result"] for test_id in ids]
    if suite_result == "fail" or "fail" in states:
        return "fail"
    if suite_result == "blocked" or "blocked" in states:
        return "blocked"
    if suite_result == "stale" or "stale" in states:
        return "stale"
    if suite_result == "not_run" or "not_run" in states:
        return "not_run"
    return "pass"


def _native_required_nodes() -> list[dict[str, str]]:
    return [{"test_id": f"INSTALL-0{number}", "product": product}
            for product in PRODUCTS for number in range(1, 6)]


def _validate_native_entry(entry: dict[str, Any], *, product: str, test_id: str,
                           target_commit: str | None, target_dirty_fingerprint: str | None,
                           evidence_dir: Path) -> tuple[str, list[str], list[dict[str, str]]]:
    requested = entry.get("result")
    allowed = {"pass", "fail", "blocked", "not_run", "stale", "aborted"}
    if requested not in allowed:
        return "fail", ["invalid_native_result_value"], []
    if requested != "pass":
        reason = entry.get("reason")
        return requested, [scrub(str(reason), 1000)] if reason else ["native_result_reason_missing"], []
    reasons = []
    if entry.get("product") != product or entry.get("test_id") != test_id:
        reasons.append("native_result_identity_mismatch")
    if not isinstance(entry.get("target_commit"), str) or entry.get("target_commit") != target_commit:
        return "stale", ["native_evidence_target_commit_mismatch"], []
    if not isinstance(entry.get("target_dirty_fingerprint"), str) or entry.get("target_dirty_fingerprint") != target_dirty_fingerprint:
        return "stale", ["native_evidence_dirty_fingerprint_mismatch"], []
    package_hash = entry.get("package_sha256")
    if not isinstance(package_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", package_hash):
        reasons.append("native_package_hash_missing_or_invalid")
    if not isinstance(entry.get("product_version"), str) or not entry["product_version"].strip():
        reasons.append("native_product_version_missing")
    observations = entry.get("observations")
    if not isinstance(observations, dict):
        reasons.append("native_observations_missing")
        observations = {}
    requirements = {
        "INSTALL-01": ("installed", "enabled", "setup_ok", "write_read_ok", "trust_state"),
        "INSTALL-02": ("session_ids", "context_restored", "data_preserved"),
        "INSTALL-03": ("version_before", "version_after", "backup_sha256", "update_ok", "rollback_tested", "restore_tested", "data_preserved"),
        "INSTALL-04": ("removed", "reinstalled", "db_id_before", "db_id_after", "data_preserved"),
        "INSTALL-05": ("lifecycle_events", "selection_saved", "claim_seen", "finish_seen", "automatic_done_after_stop", "explicit_finish_done"),
    }
    for key in requirements[test_id]:
        if key not in observations:
            reasons.append(f"observation_missing:{key}")
    if test_id == "INSTALL-01":
        for key in ("installed", "enabled", "setup_ok", "write_read_ok"):
            if observations.get(key) is not True:
                reasons.append(f"native_observation_not_true:{key}")
        if observations.get("trust_state") not in {"trusted", "not_required"}:
            reasons.append("native_trust_or_enablement_not_ready")
    elif test_id == "INSTALL-02":
        sessions = observations.get("session_ids")
        if not isinstance(sessions, list) or len(set(map(str, sessions))) < 2:
            reasons.append("two_distinct_sessions_not_proven")
        if observations.get("context_restored") is not True or observations.get("data_preserved") is not True:
            reasons.append("new_session_context_or_data_preservation_not_proven")
    elif test_id == "INSTALL-03":
        backup_hash = observations.get("backup_sha256")
        if not isinstance(backup_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", backup_hash):
            reasons.append("upgrade_backup_hash_invalid")
        if observations.get("version_before") == observations.get("version_after"):
            reasons.append("upgrade_versions_not_distinct")
        for key in ("update_ok", "rollback_tested", "restore_tested", "data_preserved"):
            if observations.get(key) is not True:
                reasons.append(f"native_observation_not_true:{key}")
    elif test_id == "INSTALL-04":
        if observations.get("db_id_before") != observations.get("db_id_after"):
            reasons.append("database_identity_not_preserved")
        for key in ("removed", "reinstalled", "data_preserved"):
            if observations.get(key) is not True:
                reasons.append(f"native_observation_not_true:{key}")
    elif test_id == "INSTALL-05":
        if observations.get("automatic_done_after_stop") is not False:
            reasons.append("stop_idle_must_not_complete_item")
        for key in ("selection_saved", "claim_seen", "finish_seen", "explicit_finish_done"):
            if observations.get(key) is not True:
                reasons.append(f"native_observation_not_true:{key}")
        if not isinstance(observations.get("lifecycle_events"), list) or not observations["lifecycle_events"]:
            reasons.append("native_lifecycle_events_missing")
    refs = entry.get("evidence_refs")
    verified_refs = []
    if not isinstance(refs, list) or not refs:
        reasons.append("native_evidence_refs_missing")
    else:
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                reasons.append("native_evidence_ref_invalid")
                continue
            relative = PurePosixPath(ref["path"].replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                reasons.append("native_evidence_ref_outside_root")
                continue
            raw_target = evidence_dir / Path(*relative.parts)
            has_link = False
            cursor = raw_target
            while cursor != evidence_dir and evidence_dir in cursor.parents:
                if cursor.is_symlink():
                    has_link = True
                    break
                cursor = cursor.parent
            if has_link:
                reasons.append("native_evidence_ref_symlink_rejected")
                continue
            target = raw_target.resolve()
            try:
                target.relative_to(evidence_dir.resolve())
                expected_hash = ref.get("sha256")
                if not target.is_file() or not isinstance(expected_hash, str) or _sha256_file(target) != expected_hash.lower():
                    reasons.append("native_evidence_ref_missing_or_hash_mismatch")
                else:
                    verified_refs.append({"path": relative.as_posix(), "sha256": expected_hash.lower()})
            except (OSError, ValueError):
                reasons.append("native_evidence_ref_unreadable_or_outside_root")
    if isinstance(package_hash, str) and package_hash.lower() not in {ref["sha256"] for ref in verified_refs}:
        reasons.append("native_package_hash_not_backed_by_verified_reference")
    return ("blocked", sorted(set(reasons)), verified_refs) if reasons else ("pass", [], verified_refs)


def parse_native_evidence(path: Path | None, target: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    required = _native_required_nodes()
    if path is None:
        return [{**item, "result": "not_run", "reasons": ["parent_native_evidence_not_supplied"], "evidence_refs": []}
                for item in required], {"status": "not_supplied"}
    if path.is_symlink() or not path.is_file():
        return [{**item, "result": "blocked", "reasons": ["native_evidence_file_missing"], "evidence_refs": []}
                for item in required], {"status": "blocked", "source": "<native-evidence>"}
    if path.stat().st_size > 256 * 1024:
        return [{**item, "result": "blocked", "reasons": ["native_evidence_file_size_limit"], "evidence_refs": []}
                for item in required], {"status": "blocked", "source": "<native-evidence>"}
    file_hash = _sha256_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        entries = value.get("results") if isinstance(value, dict) and value.get("schema_version") == 1 else None
        if not isinstance(entries, list):
            raise ValueError("schema_version=1 and results array are required")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [{**item, "result": "blocked", "reasons": ["native_evidence_json_invalid"], "evidence_refs": []}
                for item in required], {"status": "blocked", "source": "<native-evidence>", "sha256": file_hash}

    results = []
    for item in required:
        matches = [entry for entry in entries if isinstance(entry, dict) and
                   entry.get("test_id") == item["test_id"] and entry.get("product") == item["product"]]
        if not matches:
            results.append({**item, "result": "not_run", "reasons": ["no_native_result_for_product_and_test"], "evidence_refs": []})
        elif len(matches) > 1:
            results.append({**item, "result": "fail", "reasons": ["duplicate_native_result_entries"], "evidence_refs": []})
        else:
            entry = matches[0]
            state, reasons, refs = _validate_native_entry(entry, product=item["product"], test_id=item["test_id"],
                                                           target_commit=target.get("commit"),
                                                           target_dirty_fingerprint=target.get("dirty_fingerprint"),
                                                           evidence_dir=path.parent)
            observations = entry.get("observations", {})
            safe_observations = {}
            if isinstance(observations, dict):
                for key in ("installed", "enabled", "setup_ok", "write_read_ok", "trust_state",
                            "context_restored", "data_preserved", "session_ids", "version_before",
                            "version_after", "backup_sha256", "update_ok", "rollback_tested",
                            "restore_tested", "removed", "reinstalled", "db_id_before", "db_id_after",
                            "lifecycle_events", "selection_saved", "claim_seen", "finish_seen",
                            "automatic_done_after_stop", "explicit_finish_done"):
                    if key not in observations:
                        continue
                    value_for_key = observations[key]
                    if isinstance(value_for_key, str):
                        safe_observations[key] = scrub(value_for_key, 256)
                    elif isinstance(value_for_key, bool) or isinstance(value_for_key, int):
                        safe_observations[key] = value_for_key
                    elif isinstance(value_for_key, list) and all(isinstance(part, (str, int, bool)) for part in value_for_key):
                        safe_observations[key] = [scrub(str(part), 256) for part in value_for_key[:30]]
            package_hash = entry.get("package_sha256")
            product_version = entry.get("product_version")
            results.append({**item, "result": state, "reasons": reasons,
                            "target_commit": entry.get("target_commit") if isinstance(entry.get("target_commit"), str) else None,
                            "target_dirty_fingerprint": entry.get("target_dirty_fingerprint") if isinstance(entry.get("target_dirty_fingerprint"), str) else None,
                            "product_version": scrub(product_version, 128) if isinstance(product_version, str) else None,
                            "package_sha256": package_hash.lower() if isinstance(package_hash, str) and re.fullmatch(r"[0-9a-fA-F]{64}", package_hash) else None,
                            "observations": safe_observations, "evidence_refs": refs})
    return results, {"status": "read", "source": "<native-evidence>", "sha256": file_hash,
                     "schema_version": 1, "result_count": len(entries)}


def _safe_junit_copy(junit: Path, cases: list[dict[str, Any]], suite_errors: list[str]) -> dict[str, Any]:
    # Store a redacted summary rather than raw JUnit XML, which contains host/path fields.
    return {"format": "pytest-junit-summary-v1", "case_count": len(cases),
            "suite_errors": suite_errors, "cases": cases}


def _mapping_hash() -> str:
    serial = {"mapping_version": MAPPING_VERSION, "G1_ids": list(G1_IDS), "G2_ids": list(G2_IDS),
              "install_ids": list(INSTALL_IDS), "module_layers": MODULE_LAYERS,
              "test_nodes": TEST_NODE_MAP, "required_coverage": ID_REQUIREMENTS,
              "optional_platform_skip_nodes": sorted(OPTIONAL_SYMLINK_SKIP_NODES)}
    return hashlib.sha256(json.dumps(serial, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _self_test() -> None:
    xml = ET.fromstring("""<testsuites><testsuite tests="4" failures="0" skipped="2" errors="1">
      <error message="synthetic collect failure">secret token=value</error>
      <testcase classname="tests.test_queries" name="test_read_02_summary_is_bounded_and_keeps_current_warning" time="0.1" />
      <testcase classname="tests.test_hooks" name="test_pending_retained_on_cli_failure" time="0.1"><skipped message="requires permission" /></testcase>
      <testcase classname="tests.test_resources" name="test_resource_rejects_escape_and_symlink" time="0.1"><skipped message="This Windows account cannot create symlinks" /></testcase>
      <testcase classname="tests.test_queries" name="test_read_01_fake_string" time="0.1" />
    </testsuite></testsuites>""")
    cases = [_case_record(case) for case in xml.findall(".//testcase")]
    summary = summarize_ids(cases)
    suite_errors = _xml_suite_errors(xml)
    assert len(suite_errors) == 1 and "[REDACTED]" in suite_errors[0]
    assert cases[0]["layer"] == "core_unit" and cases[0]["result"] == "pass"
    assert summary["READ-02"]["result"] == "pass"
    assert cases[1]["layer"] == "hook_fixture" and cases[1]["result"] == "blocked"
    assert summary["HOOK-02"]["result"] == "blocked"
    assert cases[2]["result"] == "blocked" and cases[2]["optional_platform_skip"] is True
    assert summary["RESOURCE-03"]["result"] == "not_run"
    assert cases[3]["coverage"] == []
    assert summary["DATA-01"]["result"] == "not_run"
    assert set(ID_REQUIREMENTS) == set(G1_IDS + G2_IDS)
    for test_id, specification in ID_REQUIREMENTS.items():
        for node in specification["nodes"]:
            assert node in TEST_NODE_MAP
            assert any(cover["test_id"] == test_id and cover["complete"]
                       for cover in TEST_NODE_MAP[node].get("covers", []))
    print("verify_phase1 parser self-test: pass")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, help="User-selected .pmt-test evidence directory")
    parser.add_argument("--python", type=Path, help="Python executable; default is this interpreter")
    parser.add_argument("--native-evidence", type=Path,
                        help="Parent-authored schema_version=1 native INSTALL evidence JSON")
    parser.add_argument("--pytest-args", nargs="*", default=[], help="Additional pytest args; omitted tests remain not_run")
    parser.add_argument("--self-test", action="store_true", help="Run synthetic JUnit parser checks only")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return 0
    if args.evidence_root is None:
        parser.error("--evidence-root is required unless --self-test is used")

    project = Path(__file__).resolve().parents[1]
    evidence_root = args.evidence_root.expanduser().resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    execution_id = str(uuid.uuid4())
    destination = evidence_root / execution_id
    destination.mkdir()
    junit = destination / "pytest.xml"
    output_file = destination / "pytest-output.txt"
    summary_file = destination / "pytest-results.json"
    started = now()
    started_clock = time.monotonic()
    python = str(args.python.resolve()) if args.python else sys.executable
    basetemp = destination / "pytest-temp"
    command = [python, "-m", "pytest", "-q", "tests", f"--basetemp={basetemp}",
               f"--junitxml={junit}", *args.pytest_args]
    env = os.environ.copy()
    src = str(project / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    target_start = git_fingerprint(project)
    completed = run(command, project)
    target_end = git_fingerprint(project)
    elapsed = round(time.monotonic() - started_clock, 3)
    stdout, stderr = scrub(completed.stdout), scrub(completed.stderr)
    output_file.write_text("STDOUT\n" + stdout + "\nSTDERR\n" + stderr, encoding="utf-8")
    cases, suite_errors, skipped_count = parse_junit(junit)
    junit_hash = _sanitize_junit_file(junit)
    target_changed = (target_start.get("commit"), target_start.get("dirty_fingerprint")) != (
        target_end.get("commit"), target_end.get("dirty_fingerprint"))
    if target_changed:
        suite_errors.append("target commit or relevant dirty files changed during this execution")
        for case in cases:
            if case["result"] == "pass":
                case["result"] = "stale"
                case["failure_evidence"] = "target changed during the test execution"
    if any("No module named 'pmt'" in line or 'No module named "pmt"' in line for line in (stdout, stderr)):
        pytest_result = "blocked"
        suite_errors.append("pytest could not import the PMT package")
    elif completed.returncode == 5 and not cases:
        pytest_result = "not_run"
    elif completed.returncode != 0:
        pytest_result = "fail" if cases or suite_errors else "blocked"
    elif skipped_count:
        pytest_result = "blocked"
    elif target_changed:
        pytest_result = "stale"
    elif not cases:
        pytest_result = "not_run"
    else:
        pytest_result = "pass"
    id_results = summarize_ids(cases)
    native_results, native_source = parse_native_evidence(
        args.native_evidence.expanduser().absolute() if args.native_evidence else None,
        target_start)
    for item in native_results:
        id_results[f"{item['test_id']}:{item['product']}"] = item
    blocked_nodes = [case for case in cases if case["result"] == "blocked"]
    optional_symlink_only = bool(blocked_nodes) and skipped_count == len(blocked_nodes) and completed.returncode == 0 and all(
        case.get("optional_platform_skip", False) for case in blocked_nodes)
    if target_changed:
        phase_suite_gate = "stale"
    elif optional_symlink_only:
        phase_suite_gate = None
    else:
        phase_suite_gate = pytest_result if pytest_result in {"fail", "blocked", "stale"} else None
    g1_result = _stage_result(list(G1_IDS), id_results, phase_suite_gate)
    g2_result = _stage_result(list(G1_IDS + G2_IDS), id_results, phase_suite_gate)
    g3_result = _stage_result(list(G1_IDS + G2_IDS + INSTALL_IDS), id_results,
                              phase_suite_gate)
    try:
        pytest_version = importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError:
        pytest_version = "unavailable"
    target = target_end
    safe_summary = _safe_junit_copy(junit, cases, suite_errors)
    summary_file.write_text(json.dumps(safe_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    environment = {"os": platform.system(), "os_release": platform.release(),
                   "architecture": platform.machine(), "python": platform.python_version(),
                   "sqlite": sqlite3.sqlite_version, "pytest": pytest_version,
                   "executor": "full local pytest suite; product installs require separate native evidence"}
    mapping_fingerprint = _mapping_hash()
    safe_pytest_args = [scrub(str(value), 512) for value in args.pytest_args]
    pyproject = project / "pyproject.toml"
    dependency_fingerprint = hashlib.sha256((mapping_fingerprint +
        (hashlib.sha256(pyproject.read_bytes()).hexdigest() if pyproject.is_file() else "missing") +
        python + "|pytest=" + pytest_version + "|pytest_args=" + json.dumps(args.pytest_args, ensure_ascii=False)).encode("utf-8")).hexdigest()
    manifest: dict[str, Any] = {
        "manifest_version": 2,
        "execution_id": execution_id,
        "test_mapping_version": MAPPING_VERSION,
        "test_mapping_sha256": mapping_fingerprint,
        "at_start": started,
        "at_end": now(),
        "runtime_seconds": elapsed,
        "result": {"pytest": pytest_result, "G1": g1_result, "G2": g2_result, "G3": g3_result},
        "suite": "PMT phase-1 complete checked-in pytest suite",
        "target": {**target, "stable_during_execution": not target_changed,
                   "comparison_commit": target_start.get("commit"),
                   "comparison_dirty_fingerprint": target_start.get("dirty_fingerprint")},
        "environment": environment,
        "dependency_settings_fingerprint": dependency_fingerprint,
        "fixture_version": "tests tree at target dirty fingerprint",
        "command": ["<python>", "-m", "pytest", "-q", "tests", "--basetemp=<evidence-root>/<execution-id>/pytest-temp", "--junitxml=<evidence-root>/<execution-id>/pytest.xml", *safe_pytest_args],
        "cwd_scope": "<project-root>; per-test data/config roots are isolated by pytest fixtures",
        "exit_code": completed.returncode,
        "observed_test_nodes": cases,
        "required_test_results": [id_results[test_id] for test_id in G1_IDS + G2_IDS],
        "native_install_results": [id_results[test_id] for test_id in INSTALL_IDS],
        "native_evidence_source": native_source,
        "native_evidence_limit": "Validated metadata and referenced file hashes; human review of native UI/product behavior remains required.",
        "scope_notes": (["Windows symlink privilege skips remain blocked at their individual pytest nodes; they are optional helper cases when the separately-required Windows junction boundary test passes."]
                        if optional_symlink_only else []),
        "coverage_summary": {"G1_required": len(G1_IDS), "G2_required": len(G2_IDS),
                             "G3_install_required": len(INSTALL_IDS),
                             "mapped_test_nodes": sum(case["mapping_node"] in TEST_NODE_MAP for case in cases),
                             "unmapped_test_nodes": sum(case["mapping_node"] not in TEST_NODE_MAP for case in cases),
                             "pytest_skips_blocked": skipped_count},
        "evidence_refs": [
            {"path": "pytest.xml", "sha256": junit_hash} if junit_hash else {"path": "pytest.xml", "status": "missing"},
            {"path": "pytest-results.json", "sha256": hashlib.sha256(summary_file.read_bytes()).hexdigest()},
            {"path": "pytest-output.txt", "sha256": hashlib.sha256(output_file.read_bytes()).hexdigest()},
            {"path": "native-evidence.json", "sha256": native_source.get("sha256")} if native_source.get("sha256") else {"path": "native-evidence.json", "status": native_source["status"]},
        ],
        "evidence_policy": "No full environment dump or raw JUnit host/path fields; .pmt-test, virtualenv, logs, auth, credentials, and secret files are excluded from Git dirty hashing.",
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {state: sum(item["result"] == state for item in id_results.values()) for state in ("pass", "fail", "blocked", "not_run", "stale", "aborted")}
    print(json.dumps({"execution_id": execution_id, "pytest": pytest_result, "G1": g1_result,
                      "G2": g2_result, "G3": g3_result, "exit_code": completed.returncode,
                      "test_id_counts": counts, "evidence_dir": str(destination)}, ensure_ascii=False))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
